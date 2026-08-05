"""Antigravity image generation.

Runs Google's Antigravity CLI agent, which generates images against the user's
**Google AI Pro/Ultra subscription** — so unlike the Gemini REST API path this
costs nothing per image. We shell out with a natural-language instruction (piped
via stdin, like the Gemini CLI path) telling the agent to generate one image and
save it to an exact path, then read that file back.

Nano-Banana-class models have no native alpha, so we request a solid magenta key
color and strip it in post-processing (`needs_transparency_postprocess = True`).

NOTE: the exact Antigravity CLI invocation is not yet pinned down in this repo —
the binary and any leading flags are configurable via `ANTIGRAVITY_BIN` /
`ANTIGRAVITY_ARGS` (see config.py). The instruction-and-save-to-path contract
mirrors the Gemini/nanobanana CLI flow and should adapt to most agent CLIs; if
the installed CLI wants different flags, set them there rather than editing code.
"""
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import config
from ..prompting import CHROMA_HINT, NO_RESIZE_HINT
from .base import ImageProvider, ProviderError

TIMEOUT_S = 300


from . import prefs

import shlex

class AntigravityProvider(ImageProvider):
    name = "antigravity"
    needs_transparency_postprocess = True

    def generate(
        self,
        prompt: str,
        out_path: Path,
        size: str = "1024x1024",
        reference_images: list[Path] | None = None,
        transparent: bool = True,
        model: str | None = None,
        visual_model: str | None = None,
        reference_mode: bool = False,
        params: dict | None = None,
    ) -> Path:
        # `params` is Higgsfield-native per-model tuning (resolution/quality/aspect_ratio);
        # the Antigravity CLI takes no equivalent flags, so it is accepted and ignored
        # rather than raising — callers shouldn't have to branch on provider to build a call.
        exe = shutil.which(config.ANTIGRAVITY_BIN)
        if not exe:
            raise ProviderError(
                f"Antigravity CLI not found on PATH (looked for "
                f"'{config.ANTIGRAVITY_BIN}'). Install it and sign in with your "
                f"Google account, or set ANTIGRAVITY_BIN in server/.env."
            )

        model = model or prefs.get_provider_model("antigravity") or "gemini-3.6-flash-high"
        visual_model = visual_model or prefs.get_provider_visual_model("antigravity") or "auto"

        with tempfile.TemporaryDirectory(prefix="antigravity-") as tmp:
            tmpdir = Path(tmp)
            target = tmpdir / "output.png"

            local_refs = []
            for i, ref in enumerate(reference_images or [], start=1):
                p = Path(ref)
                if p.exists():
                    ref_label = f"crop_reference_{i}" if i == 1 else f"style_reference_{i}"
                    dest = tmpdir / f"{ref_label}{p.suffix.lower() or '.png'}"
                    dest.write_bytes(p.read_bytes())
                    local_refs.append(dest.name)

            visual_hint = ""
            if visual_model and visual_model != "auto":
                visual_hint = f" Use the {visual_model} image generation model."
            instruction = f"Generate one image.{visual_hint} Image description: {prompt}"
            if transparent:
                instruction += CHROMA_HINT
            if local_refs:
                primary_ref = local_refs[0]
                names = ", ".join(local_refs)
                if reference_mode:
                    # `prompt` (prompting.reference_instruction) already fully explains
                    # what to do with the reference — up to and including exactly which
                    # things are allowed to change. Appending the from-scratch framing
                    # below on top of that stacks a second, uncoordinated instruction: it
                    # was measured actively fighting reference_instruction's "reproduce
                    # exactly, change ONLY X" — a bar that isn't meant to be symmetric came
                    # back redesigned, and a stray fill-noise blemish the caller explicitly
                    # asked to have removed came back preserved as "exact design detail".
                    instruction += (
                        f" Reference image file(s) are in your current working directory: "
                        f"{names}. Open and inspect {primary_ref} — it is what the "
                        f"description above refers to as \"the reference\"."
                    )
                else:
                    instruction += (
                        f" Reference image files are in your current working directory: {names}. "
                        f"Open and inspect {primary_ref} (the cropped reference image). "
                        f"Take the element in {primary_ref} and extract it exactly as depicted in the reference image, "
                        f"preserving its exact shapes, artwork, details, colors, and design, but completely removed from its original background — "
                        f"render the extracted element cleanly isolated on a solid uniform pure magenta background (#FF00FF)."
                    )
            instruction += (
                f" Save the generated image to exactly this path: {target}.{NO_RESIZE_HINT} "
                f"Do not ask questions; just generate and save the file."
            )

            cmd = [exe, "--dangerously-skip-permissions", "--print-timeout", "240s"]
            if model:
                cmd.extend(["--model", model])
            if config.ANTIGRAVITY_ARGS:
                cmd.extend(config.ANTIGRAVITY_ARGS.split())
            cmd.extend(["-p", instruction])
            self.last_command = shlex.join(cmd)
            started = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_S,
                    cwd=tmpdir,
                    shell=False,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                raise ProviderError(f"Antigravity CLI timed out after {TIMEOUT_S}s")
            except OSError as e:
                raise ProviderError(f"Failed to run Antigravity CLI: {e}")

            result = target if target.exists() else self._newest_png(tmpdir, started, exclude=set(local_refs))
            if result is None:
                tail = (proc.stdout + "\n" + proc.stderr).strip()[-800:]
                raise ProviderError(
                    f"Antigravity CLI did not produce an image (likely hit a quota/rate "
                    f"limit or refused). Output tail:\n{tail}"
                )
            out_path.write_bytes(result.read_bytes())
        return out_path

    @staticmethod
    def _newest_png(directory: Path, since_ts: float, exclude: set[str] | None = None) -> Path | None:
        # Reference images (crop + siblings) are copied into this same tmpdir before
        # generation starts, so they must never qualify as "the generated output" —
        # otherwise a quota/refusal failure silently reuses the crop as the asset
        # instead of surfacing an error.
        exclude = exclude or set()
        pngs = [
            p for p in directory.rglob("*.png")
            if p.name not in exclude and p.stat().st_mtime >= since_ts - 1
        ]
        return max(pngs, key=lambda p: p.stat().st_mtime) if pngs else None
