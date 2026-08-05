"""Lazy loaders and a model cache for the local extraction/repair models.

Three rules hold everywhere in here:

1. **Nothing loads at import.** The server must boot, and every non-ML endpoint must
   keep working, on a machine with no torch installed. Each accessor imports its library
   inside the function and raises `MlUnavailable` if it isn't there; callers catch that
   and fall back to a classical path.
2. **Load once, reuse.** Model construction dominates the cost of a small crop, so each
   model is a process-wide singleton behind a lock (generation runs up to 3 regions
   concurrently and would otherwise build three SAM2s and exhaust 8GB of VRAM).
3. **Weights live under `storage/models/`,** downloaded on first use, never vendored.

Device selection is automatic (CUDA when present, else CPU) and overridable with
`ML_DEVICE` in server/.env for debugging.
"""
from __future__ import annotations

import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import STORAGE_DIR

MODELS_DIR = STORAGE_DIR / "models"

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}


class MlUnavailable(RuntimeError):
    """A local model was requested but its library or weights aren't available.

    Always recoverable: every caller has a classical fallback, so this is control flow
    for choosing a backend, not an error to surface to the user."""


@dataclass(frozen=True)
class ModelSpec:
    key: str
    filename: str
    url: str
    size_mb: int


# sam2.1_hiera_small: the accuracy/VRAM sweet spot for box-prompted UI segmentation —
# tiny loses precision on thin borders and bevels, large needs far more VRAM for no
# meaningful gain on flat 2D art. No longer the model actually asked for a mask — see
# HQSAM_CHECKPOINT below — but the config/build code stays in case HQ-SAM becomes
# unavailable in some environment and a caller wants the old backend back explicitly.
SAM2_CHECKPOINT = ModelSpec(
    key="sam2",
    filename="sam2.1_hiera_small.pt",
    url="https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt",
    size_mb=184,
)
SAM2_CONFIG = "configs/sam2.1/sam2.1_hiera_s.yaml"

# HQ-SAM vit_tiny: same box-prompted role as SAM2 above, but with a learnable
# high-quality output token that keeps thin/hollow shapes (a crossed-blade icon's notch,
# a hollow arrow's inside edge) intact instead of rounding them into a blob — the one
# failure mode SAM2's own decoder could not be prompted, jittered, or iteratively
# refined out of (see processing/extract.py:sam2_mask). vit_tiny specifically: ~3.7GB
# VRAM, well inside the budget `SAM2_CHECKPOINT`'s comment above was written for, and
# measured no worse than SAM2 on every other region in the ClashUp lobby.
HQSAM_CHECKPOINT = ModelSpec(
    key="hqsam",
    filename="sam_hq_vit_tiny.pth",
    url="https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_tiny.pth",
    size_mb=41,
)

# The anime-trained Real-ESRGAN variant. Deliberately not the photo model: stylised game
# UI is flat colour with hard outlines, exactly what anime6B is tuned for, while the
# photo model hallucinates texture into flat fills. Loaded into a local RRDBNet (see
# processing/upres.py) rather than through basicsr, which pins old torch.
REALESRGAN = ModelSpec(
    key="realesrgan",
    filename="RealESRGAN_x4plus_anime_6B.pth",
    url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
    size_mb=18,
)
REALESRGAN_ONNX = REALESRGAN  # backwards-compatible alias


def device() -> str:
    """'cuda' when a GPU is usable, else 'cpu'. Override with ML_DEVICE in server/.env."""
    forced = os.getenv("ML_DEVICE", "").strip().lower()
    if forced:
        return forced
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def download(spec: ModelSpec, progress=None) -> Path:
    """Fetch a model's weights into storage/models/ if not already there.

    Downloads to a .part file and renames on completion, so an interrupted download can
    never leave a truncated file that later loads as a corrupt model."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / spec.filename
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    if progress:
        progress(f"Downloading {spec.filename} (~{spec.size_mb}MB) — one time only…")
    try:
        with urllib.request.urlopen(spec.url, timeout=120) as resp, open(tmp, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise MlUnavailable(f"Could not download {spec.filename}: {e}") from e
    tmp.replace(dest)
    return dest


def is_cached(spec: ModelSpec) -> bool:
    path = MODELS_DIR / spec.filename
    return path.exists() and path.stat().st_size > 0


def get_sam2_predictor(progress=None):
    """Process-wide SAM2 image predictor. Raises MlUnavailable when torch/sam2 or the
    weights can't be had — callers fall back to GrabCut."""
    with _LOCK:
        if "sam2" in _CACHE:
            return _CACHE["sam2"]
        try:
            import torch
            from sam2.build_sam import build_sam2
            from sam2.sam2_image_predictor import SAM2ImagePredictor
        except ImportError as e:
            raise MlUnavailable(f"SAM2 is not installed: {e}") from e

        ckpt = download(SAM2_CHECKPOINT, progress=progress)
        dev = device()
        if progress:
            progress(f"Loading SAM2 on {dev}…")
        try:
            model = build_sam2(SAM2_CONFIG, str(ckpt), device=dev)
            predictor = SAM2ImagePredictor(model)
        except Exception as e:
            raise MlUnavailable(f"Could not initialise SAM2: {e}") from e
        if dev == "cuda":
            torch.cuda.empty_cache()
        _CACHE["sam2"] = predictor
        return predictor


def get_hqsam_predictor(progress=None):
    """Process-wide HQ-SAM (vit_tiny) image predictor — the box-prompted segmenter
    `processing/extract.py:sam2_mask` actually calls. Raises MlUnavailable when the
    library or weights can't be had — callers fall back to GrabCut, same as SAM2 did."""
    with _LOCK:
        if "hqsam" in _CACHE:
            return _CACHE["hqsam"]
        try:
            import torch
            from segment_anything_hq import sam_model_registry, SamPredictor
        except ImportError as e:
            raise MlUnavailable(f"HQ-SAM is not installed: {e}") from e

        ckpt = download(HQSAM_CHECKPOINT, progress=progress)
        dev = device()
        if progress:
            progress(f"Loading HQ-SAM on {dev}…")
        try:
            model = sam_model_registry["vit_tiny"](checkpoint=str(ckpt))
            model.to(device=dev)
            model.eval()
            predictor = SamPredictor(model)
        except Exception as e:
            raise MlUnavailable(f"Could not initialise HQ-SAM: {e}") from e
        if dev == "cuda":
            torch.cuda.empty_cache()
        _CACHE["hqsam"] = predictor
        return predictor


def get_lama(progress=None):
    """Process-wide LaMa inpainter. simple-lama-inpainting fetches its own weights on
    first construction, so there's no explicit download step here."""
    with _LOCK:
        if "lama" in _CACHE:
            return _CACHE["lama"]
        try:
            from simple_lama_inpainting import SimpleLama
        except ImportError as e:
            raise MlUnavailable(f"simple-lama-inpainting is not installed: {e}") from e
        if progress:
            progress("Loading LaMa inpainting model…")
        try:
            lama = SimpleLama(device=device())
        except Exception as e:
            raise MlUnavailable(f"Could not initialise LaMa: {e}") from e
        _CACHE["lama"] = lama
        return lama


def get_onnx_session(model_path: Path, progress=None):
    """ONNX Runtime session on GPU when available. Used by the super-resolution path."""
    key = f"onnx:{model_path}"
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise MlUnavailable(f"onnxruntime is not installed: {e}") from e
        if not model_path.exists():
            raise MlUnavailable(f"ONNX model not found: {model_path}")
        available = ort.get_available_providers()
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in available]
        if progress:
            progress(f"Loading {model_path.name} via {providers[0]}…")
        session = ort.InferenceSession(str(model_path), providers=providers)
        _CACHE[key] = session
        return session


def unload_all() -> None:
    """Drop every cached model and free VRAM. For tests and for recovering from an OOM."""
    with _LOCK:
        _CACHE.clear()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def status() -> dict:
    """What the ML stack can currently do — surfaced on /api/status so the UI can explain
    why a run fell back to the classical path instead of silently producing worse output."""
    info: dict = {
        "torch": torch_available(),
        "device": device(),
        "models_dir": str(MODELS_DIR),
        "cached": {
            "sam2": is_cached(SAM2_CHECKPOINT),
            "hqsam": is_cached(HQSAM_CHECKPOINT),
            "realesrgan": is_cached(REALESRGAN_ONNX),
        },
        "loaded": sorted(k for k in _CACHE),
    }
    try:
        import torch
        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / 1024 ** 3, 1
            )
    except Exception:
        pass
    for lib in ("sam2", "segment_anything_hq", "simple_lama_inpainting", "onnxruntime"):
        try:
            __import__(lib)
            info[lib] = True
        except ImportError:
            info[lib] = False
    return info
