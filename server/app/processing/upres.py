"""Super-resolution for extracted sprites.

A phone screenshot is typically 768-1080px wide, so an element that occupies 25% of it
crops to ~200px. The same element on a 1440px-wide device needs ~360px, and a nav bar
needs the full 1440. Extraction gives perfect colour but only the resolution the
screenshot had, so something has to bridge the gap.

Lanczos is the wrong bridge. Measured on the ClashUp lobby, extracted assets scored 74-98
against the reference at native size and 32-76 after `fit_to_resolution` Lanczos-upscaled
them to their 1440px-device targets — the upscale-then-display round trip destroyed a
third of the fidelity the extraction had just won.

Real-ESRGAN's anime variant is trained on exactly this material: flat colour fields, hard
outlines, no photographic texture. It reconstructs crisp edges instead of blurring them,
and it hallucinates nothing structural, so the colour fidelity extraction won is kept.

The architecture is implemented here rather than pulled from `basicsr`, which pins old
torch and drags a large tree in for one 18MB checkpoint.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..ml import MlUnavailable

# Upscaling below this factor isn't worth a model pass — Lanczos is visually equivalent
# and far cheaper.
MIN_SCALE = 1.25
# The model is x4. Anything beyond that needs tiling/repeat passes; past this we accept a
# Lanczos finish on top rather than chaining hallucination.
MODEL_SCALE = 4
# Tile size for large inputs, so a 1440px nav bar doesn't spike VRAM.
#
# 192, not 512, and the difference is not a tuning preference — it is the difference
# between seconds and not finishing. The RRDB trunk runs its last two convolutions at the
# OUTPUT resolution, so a 512+64 tile means 576x576 in and 2304x2304 out, and a single
# 64-channel feature map at that size is 1.3GB. Several of those overflow an 8GB card, and
# Windows answers an overflow by spilling CUDA allocations into system RAM rather than
# raising: a whole-screen x4 pass sat at 7891/8192 MiB and 100% GPU for over ten minutes
# before being killed. At 192 the same 768x1376 screenshot finishes in 5.7 seconds.
#
# Tiles are cheap and seams are handled by TILE_PAD, so there is no reason to run near the
# limit. If this is ever raised again, measure `nvidia-smi` during a full-screen pass — the
# symptom is not an error, it is silent thrashing.
TILE = 192
TILE_PAD = 16

_CACHE: dict[str, object] = {}


def _build_rrdbnet():
    """RRDBNet as used by RealESRGAN_x4plus_anime_6B (6 blocks, 64 feat, 32 grow).

    Standard ESRGAN architecture (Wang et al. 2018); kept minimal and dependency-free so
    the official checkpoint loads without basicsr.
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class ResidualDenseBlock(nn.Module):
        def __init__(self, nf=64, gc=32):
            super().__init__()
            self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1)
            self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1)
            self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1)
            self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
            self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, nf, gc=32):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(nf, gc)
            self.rdb2 = ResidualDenseBlock(nf, gc)
            self.rdb3 = ResidualDenseBlock(nf, gc)

        def forward(self, x):
            return self.rdb3(self.rdb2(self.rdb1(x))) * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self, in_ch=3, out_ch=3, nf=64, nb=6, gc=32):
            super().__init__()
            self.conv_first = nn.Conv2d(in_ch, nf, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB(nf, gc) for _ in range(nb)])
            self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_up2 = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1)
            self.conv_last = nn.Conv2d(nf, out_ch, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            feat = self.conv_first(x)
            feat = feat + self.conv_body(self.body(feat))
            feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
            feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(feat)))

    return RRDBNet()


def _get_model():
    from .. import ml

    if "realesrgan" in _CACHE:
        return _CACHE["realesrgan"]
    try:
        import torch
    except ImportError as e:
        raise MlUnavailable(f"torch is not installed: {e}") from e

    ckpt_path = ml.download(ml.REALESRGAN_ONNX)
    model = _build_rrdbnet()
    state = torch.load(str(ckpt_path), map_location="cpu", weights_only=True)
    state = state.get("params_ema") or state.get("params") or state
    try:
        model.load_state_dict(state, strict=True)
    except RuntimeError as e:
        raise MlUnavailable(f"Real-ESRGAN checkpoint did not match the architecture: {e}") from e
    model.eval().to(ml.device())
    _CACHE["realesrgan"] = model
    return model


def _run_model(rgb: np.ndarray) -> np.ndarray:
    """x4 super-resolution of an HxWx3 uint8 array, tiled to bound VRAM."""
    import torch

    from .. import ml

    model = _get_model()
    dev = ml.device()
    h, w = rgb.shape[:2]
    out = np.zeros((h * MODEL_SCALE, w * MODEL_SCALE, 3), np.uint8)

    with torch.no_grad():
        for y0 in range(0, h, TILE):
            for x0 in range(0, w, TILE):
                y1, x1 = min(h, y0 + TILE), min(w, x0 + TILE)
                # Pad each tile so seams fall outside the region we keep.
                py0, px0 = max(0, y0 - TILE_PAD), max(0, x0 - TILE_PAD)
                py1, px1 = min(h, y1 + TILE_PAD), min(w, x1 + TILE_PAD)
                tile = rgb[py0:py1, px0:px1]

                t = torch.from_numpy(tile.astype(np.float32) / 255.0)
                t = t.permute(2, 0, 1).unsqueeze(0).to(dev)
                sr = model(t).clamp_(0, 1).squeeze(0).permute(1, 2, 0).cpu().numpy()
                sr = (sr * 255.0).round().astype(np.uint8)

                # Drop the padding, in output coordinates.
                ky0, kx0 = (y0 - py0) * MODEL_SCALE, (x0 - px0) * MODEL_SCALE
                kh, kw = (y1 - y0) * MODEL_SCALE, (x1 - x0) * MODEL_SCALE
                out[y0 * MODEL_SCALE:y1 * MODEL_SCALE, x0 * MODEL_SCALE:x1 * MODEL_SCALE] = \
                    sr[ky0:ky0 + kh, kx0:kx0 + kw]
    return out


def supersample(img: Image.Image) -> tuple[np.ndarray, int]:
    """The whole screenshot at MODEL_SCALE, as an RGB array. Falls back to (original, 1).

    Extraction runs on this rather than on the screenshot, and that single change is what
    makes edges crisp. A phone screenshot is ~768px wide, so a nav icon crops to under 100
    pixels and the dark keyline the artwork outlines it with is ONE pixel — there is no
    sub-pixel information left for any matte to find, and `fit_to_resolution` then enlarges
    that one soft pixel to fill a 160px slot, which is where the stair-stepping comes from.
    Segmenting at x4 gives the boundary sixteen times the evidence and leaves the sprite
    natively larger than its export target, so the last resize is a DOWNsample.

    Returns the scale actually achieved so callers can map their boxes onto it; a machine
    without torch gets 1 and the pipeline degrades to what it did before rather than
    failing.
    """
    rgb = np.asarray(img.convert("RGB"))
    try:
        return _run_model(rgb), MODEL_SCALE
    except Exception:
        return rgb, 1


def supersample_cached(path, img: Image.Image | None = None) -> tuple[np.ndarray, int]:
    """`supersample`, memoised on disk beside the source.

    A screenshot is supersampled once and then read by every region on it — fifteen for the
    ClashUp lobby — so without this the 5.7s model pass would be paid fifteen times per
    build. Keyed on the source's mtime and size so replacing a mockup image invalidates it
    without anyone having to remember to.
    """
    from pathlib import Path

    src = Path(path)
    try:
        stat = src.stat()
        key = f"{src.stem}-x{MODEL_SCALE}-{int(stat.st_mtime)}-{stat.st_size}.png"
        cache = src.with_name(key)
    except OSError:
        cache = None

    if cache is not None and cache.exists():
        try:
            with Image.open(cache) as cached:
                return np.asarray(cached.convert("RGB")), MODEL_SCALE
        except Exception:
            cache.unlink(missing_ok=True)  # truncated or corrupt: regenerate

    if img is None:
        with Image.open(src) as opened:
            img = opened.convert("RGB")
    big, scale = supersample(img)

    if cache is not None and scale != 1:
        # Write-then-rename, so a crash mid-write can't leave a partial PNG that later
        # loads as a blank screenshot and silently ruins every asset built from it.
        tmp = cache.with_suffix(".part")
        try:
            # `format=` is not optional: PIL infers the encoder from the extension, and
            # ".part" is not one it knows, so this raised ValueError on every single call
            # and the except below swallowed it. The cache was never written once — every
            # region on a screenshot was paying its own full-screen Real-ESRGAN pass, which
            # is exactly what this function exists to prevent.
            Image.fromarray(big).save(tmp, format="PNG")
            tmp.replace(cache)
        except Exception:
            tmp.unlink(missing_ok=True)
    return big, scale


def upres_to(img: Image.Image, target_w: int, target_h: int) -> tuple[Image.Image, str]:
    """Enlarge `img` toward (target_w, target_h), preserving aspect ratio.

    Returns (image, method) where method is "esrgan", "lanczos" or "none". Alpha is
    carried through separately — the model is RGB-only, and running it on a premultiplied
    or naively-stacked alpha channel produces coloured fringing on the matte extraction
    just worked to get clean.
    """
    w, h = img.size
    scale = min(target_w / w, target_h / h)
    if scale <= 1.0:
        return img, "none"
    if scale < MIN_SCALE:
        return img.resize((round(w * scale), round(h * scale)), Image.LANCZOS), "lanczos"

    rgba = img.convert("RGBA")
    arr = np.asarray(rgba)
    rgb, alpha = arr[..., :3], arr[..., 3]

    try:
        sr_rgb = _run_model(rgb)
    except (MlUnavailable, Exception):
        # Any failure — no torch, bad checkpoint, CUDA OOM — degrades to Lanczos rather
        # than failing the asset. The caller still gets a correctly sized sprite.
        return img.resize((round(w * scale), round(h * scale)), Image.LANCZOS), "lanczos"

    # Alpha is upscaled separately with a smooth filter: the matte is a soft-edged mask,
    # not texture, and running SR on it would sharpen the feather into stair-steps.
    sr_alpha = np.asarray(
        Image.fromarray(alpha).resize(
            (sr_rgb.shape[1], sr_rgb.shape[0]), Image.LANCZOS
        )
    )
    out = Image.fromarray(np.dstack([sr_rgb, sr_alpha]), "RGBA")

    final = (max(1, round(w * scale)), max(1, round(h * scale)))
    if out.size != final:
        out = out.resize(final, Image.LANCZOS)
    return out, "esrgan"
