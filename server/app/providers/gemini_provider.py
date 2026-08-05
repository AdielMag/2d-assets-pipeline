"""Gemini image generation.

Two paths, selected at call time:
- **API key** (`GEMINI_API_KEY` set): calls the Generative Language REST API
  (`{model}:generateContent`) directly and extracts the inline PNG. Preferred.
- **CLI fallback** (no key): runs the Gemini CLI + official Nano Banana extension
  using the user's Google-account login, telling it to save the PNG to an exact path.

Nano Banana has no native alpha either way, so we request a solid magenta key color
and strip it in post-processing (`needs_transparency_postprocess = True`).
"""
import base64
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from ..prompting import CHROMA_HINT, NO_RESIZE_HINT
from .base import ImageProvider, ProviderError

TIMEOUT_S = 300

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
API_TIMEOUT_S = 120
MAX_RETRIES = 6  # 429 backoff attempts
MAX_BACKOFF_S = 60.0


class GeminiProvider(ImageProvider):
    name = "gemini"
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
    ) -> Path:
        # Neither path below appends its own framing for what to do with a reference —
        # the CLI path's "use as style/content reference" is neutral and the API path just
        # attaches the image inline — so `reference_mode` needs no branching here; it only
        # matters to a provider that has its own from-scratch-flavoured boilerplate (see
        # AntigravityProvider.generate).
        if config.GEMINI_API_KEY:
            return self._generate_api(
                prompt, out_path, reference_images=reference_images,
                transparent=transparent, model=model,
            )
        return self._generate_cli(
            prompt, out_path, reference_images=reference_images, transparent=transparent
        )

    # --- REST API path (GEMINI_API_KEY) -------------------------------------
    def _generate_api(
        self,
        prompt: str,
        out_path: Path,
        reference_images: list[Path] | None = None,
        transparent: bool = True,
        model: str | None = None,
    ) -> Path:
        parts: list[dict] = [{"text": prompt + (CHROMA_HINT if transparent else "")}]
        for ref in reference_images or []:
            data = base64.b64encode(Path(ref).read_bytes()).decode("ascii")
            mime = "image/png" if str(ref).lower().endswith(".png") else "image/jpeg"
            parts.append({"inlineData": {"mimeType": mime, "data": data}})

        body = json.dumps({"contents": [{"parts": parts}]}).encode("utf-8")
        url = API_URL.format(model=model or config.GEMINI_IMAGE_MODEL)

        for attempt in range(MAX_RETRIES + 1):
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": config.GEMINI_API_KEY,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < MAX_RETRIES:
                    time.sleep(self._retry_delay(detail, attempt))
                    continue
                raise ProviderError(
                    f"Gemini API error {e.code}: {self._error_message(detail)}"
                )
            except urllib.error.URLError as e:
                raise ProviderError(f"Gemini API request failed: {e.reason}")

        png = self._extract_png(payload)
        if png is None:
            raise ProviderError(
                f"Gemini API returned no image. {self._extract_text(payload)}".strip()
            )
        out_path.write_bytes(png)
        return out_path

    @staticmethod
    def _retry_delay(detail: str, attempt: int) -> float:
        """Honor the API's RetryInfo.retryDelay (e.g. '59s') if present, else backoff."""
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', detail)
        if m:
            return min(float(m.group(1)) + 1.0, MAX_BACKOFF_S)
        return min(2.0 * (2 ** attempt), MAX_BACKOFF_S)

    @staticmethod
    def _error_message(detail: str) -> str:
        try:
            return json.loads(detail).get("error", {}).get("message", detail)[:300]
        except (json.JSONDecodeError, AttributeError):
            return detail[:300]

    @staticmethod
    def _extract_png(payload: dict) -> bytes | None:
        for cand in payload.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        return None

    @staticmethod
    def _extract_text(payload: dict) -> str:
        texts = []
        for cand in payload.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if part.get("text"):
                    texts.append(part["text"])
        return " ".join(texts)[:300]

    # --- CLI path (Nano Banana extension, no key) ---------------------------
    def _generate_cli(
        self,
        prompt: str,
        out_path: Path,
        reference_images: list[Path] | None = None,
        transparent: bool = True,
    ) -> Path:
        gemini = shutil.which("gemini")
        if not gemini:
            raise ProviderError(
                "No GEMINI_API_KEY set and gemini CLI not found on PATH"
            )

        with tempfile.TemporaryDirectory(prefix="nanobanana-") as tmp:
            tmpdir = Path(tmp)
            target = tmpdir / "output.png"
            instruction = (
                f"Use the nanobanana image generation tool to generate one image. "
                f"Image description: {prompt}"
            )
            if transparent:
                instruction += CHROMA_HINT
            if reference_images:
                refs = ", ".join(str(p) for p in reference_images)
                instruction += (
                    f" Use the following image file(s) as style/content reference: {refs}."
                )
            instruction += (
                f" Save the generated image to exactly this path: {target}.{NO_RESIZE_HINT} "
                f"Do not ask questions; just generate and save the file."
            )

            started = time.time()
            try:
                # Instruction is piped via stdin: npm .cmd shims mangle long/multi-line
                # argv arguments on Windows.
                proc = subprocess.run(
                    [gemini, "--approval-mode", "yolo"],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_S,
                    cwd=tmpdir,
                    shell=False,
                    input=instruction,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                raise ProviderError(f"Gemini CLI timed out after {TIMEOUT_S}s")
            except OSError as e:
                raise ProviderError(f"Failed to run gemini CLI: {e}")

            result = target if target.exists() else self._newest_png(tmpdir, started)
            if result is None:
                tail = (proc.stdout + "\n" + proc.stderr).strip()[-800:]
                raise ProviderError(
                    f"Gemini CLI did not produce an image. Output tail:\n{tail}"
                )
            out_path.write_bytes(result.read_bytes())
        return out_path

    @staticmethod
    def _newest_png(directory: Path, since_ts: float) -> Path | None:
        pngs = [
            p for p in directory.rglob("*.png") if p.stat().st_mtime >= since_ts - 1
        ]
        return max(pngs, key=lambda p: p.stat().st_mtime) if pngs else None
