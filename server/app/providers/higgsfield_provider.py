"""Higgsfield image generation via the official `higgsfield` CLI.

A purchased Higgsfield *plan* (Starter/Plus/Ultra — credits + "unlimited" models)
is a consumer-account product, separate from Higgsfield's pay-per-credit developer
API-key product. The account-based CLI (`npm i -g @higgsfield/cli`, authenticated
with `higgsfield auth login`) is how a plan is actually used programmatically — this
mirrors AntigravityProvider: shell out to a CLI signed in with the user's own
account, no API key involved.

One-time setup the user must do themselves (this code can't do OAuth login or pick
a billing account on their behalf):
    npm i -g @higgsfield/cli
    higgsfield auth login            # browser OAuth PKCE
    higgsfield workspace set <id>    # if `account status` asks for one

Soul-class models have no documented native alpha channel, so — same as
Antigravity/Gemini — we ask for a solid magenta key color and strip it in
post-processing (`needs_transparency_postprocess = True`).

Reference images ARE supported here (unlike the developer REST API this provider
used to target): `generate create <model> --image-references <path>` auto-uploads a
local file and hands it to the model as an image input.
"""
import calendar
import io
import json
import math
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from ..prompting import CHROMA_HINT
from .base import ImageProvider, ProviderError

TIMEOUT_S = 300
COST_TIMEOUT_S = 20
SPEC_TIMEOUT_S = 10
LIST_TIMEOUT_S = 30
# How many recent image jobs to search when recovering. The account's own history, newest
# first — a run that died mid-step is recovered from within its last few dozen jobs.
RECOVER_LIST_SIZE = 50
# Clock slack between this machine and Higgsfield's timestamps, applied to the lower bound
# of the recovery window. Generous because being a minute too permissive only widens the
# candidate set (which the reference check and the uniqueness rule then narrow), while
# being a second too strict discards the very job we are looking for.
RECOVER_CLOCK_SLACK_S = 120
# Statuses that mean the job produced nothing and never will. Everything else — completed,
# queued, whatever a future build calls "still running" — is a job worth claiming, since
# the credits are already committed either way.
DEAD_STATUSES = {"failed", "canceled", "cancelled", "error", "rejected", "nsfw"}

from . import pending, prefs

# `higgsfield model get <job_type>` declares each model's accepted params (name, type,
# enum, default). Cached because a sweep asks for the same handful of models hundreds of
# times and the answer is a static property of the model, not of the request.
_SPEC_TTL_S = 600.0
_spec_cache: dict[str, tuple[float, dict]] = {}
_spec_lock = threading.Lock()

# Marker value for `params["aspect_ratio"]`: derive the ratio from the reference image's
# own pixel dimensions instead of hardcoding one. Every model defaults to 1:1, so a
# reference-mode redraw of a 2.6:1 button is drawn on a square canvas and has to be
# trimmed and padded back into shape afterwards (see mockups._redraw_built_regions) —
# asking for the right canvas up front is free and skips that lossy round trip.
AUTO_ASPECT = "auto"


def _model_spec(exe: str, model: str) -> dict:
    """`model get <job_type>`'s param declarations, keyed by param name. {} if unknown —
    callers treat an unavailable spec as "pass what you were given, unvalidated", so a CLI
    hiccup degrades to today's behaviour rather than dropping the caller's params."""
    now = time.time()
    with _spec_lock:
        hit = _spec_cache.get(model)
        if hit and now - hit[0] < _SPEC_TTL_S:
            return hit[1]
    try:
        proc = subprocess.run(
            [exe, "--json", "model", "get", model],
            capture_output=True, text=True, timeout=SPEC_TIMEOUT_S,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
        payload = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        payload = {}
    spec = {
        p["name"]: p
        for p in (payload.get("params") or [])
        if isinstance(p, dict) and p.get("name")
    }
    with _spec_lock:
        _spec_cache[model] = (now, spec)
    return spec


def _snap_aspect_ratio(width: int, height: int, allowed: list[str]) -> str | None:
    """The declared `aspect_ratio` enum entry closest to a real w:h.

    Compared in log space so a ratio and its reciprocal are equidistant from square —
    picking by raw difference biases toward the landscape end of the enum and would snap a
    tall 2:3 icon to 1:1 more readily than a wide 3:2 one."""
    if not allowed or width <= 0 or height <= 0:
        return None
    target = math.log(width / height)
    best, best_err = None, None
    for opt in allowed:
        try:
            a, b = opt.split(":")
            err = abs(math.log(float(a) / float(b)) - target)
        except (ValueError, ZeroDivisionError):
            continue
        if best_err is None or err < best_err:
            best, best_err = opt, err
    return best


def _param_flags(
    exe: str, model: str, params: dict | None, reference_images: list[Path] | None
) -> list[str]:
    """Turn `params` into `--name value` CLI flags, dropping any the model doesn't declare.

    Dropping rather than erroring is deliberate: a sweep runs one config across several
    models that accept different knobs (`--quality` on Seedream, `--resolution` on FLUX,
    neither on Nano Banana), and the CLI rejects an undeclared flag outright. Silently
    omitting what a model can't take means one config description works across the pool.
    """
    if not params:
        return []
    spec = _model_spec(exe, model)
    flags: list[str] = []
    for name, value in params.items():
        if value is None:
            continue
        if spec and name not in spec:
            continue
        if name == "aspect_ratio" and value == AUTO_ASPECT:
            refs = [Path(r) for r in (reference_images or [])]
            ref = next((r for r in refs if r.exists()), None)
            if ref is None:
                continue
            try:
                from PIL import Image

                with Image.open(ref) as im:
                    size = im.size
            except Exception:
                continue
            value = _snap_aspect_ratio(size[0], size[1], (spec.get(name) or {}).get("enum") or [])
            if not value:
                continue
        # Repeated rather than comma-joined: that is how the CLI's own array-valued flags
        # (--image-references) are passed.
        for item in value if isinstance(value, (list, tuple)) else [value]:
            flags.extend([f"--{name}", str(item)])
    return flags


def _parse_ts(value) -> float:
    """Epoch seconds from the CLI's RFC3339 timestamps ("2026-08-03T17:45:09.88527Z").

    Hand-parsed rather than `datetime.fromisoformat`, which on 3.10 rejects both the
    trailing Z and a 5-digit fractional part — exactly what this API emits. Unparseable
    input returns 0.0, which reads as "older than any window" and so is simply not matched.
    Read as UTC: an offset-bearing timestamp from some future build would shift the window,
    which the reference check and the consumed-job list still guard against.
    """
    if not isinstance(value, str):
        return 0.0
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?", value)
    if not m:
        return 0.0
    y, mo, d, hh, mm, ss, frac = m.groups()
    base = calendar.timegm((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, 0))
    return base + (float("0." + frac) if frac else 0.0)


# Mean per-channel difference (0-255) below which two decoded images are treated as the
# same picture. Not zero: an upload may come back re-encoded, so identical pixels are not
# guaranteed even when the file is ours. Small enough that two different pieces of UI
# artwork never land inside it — measured on the crops this pipeline produces, unrelated
# elements sit two orders of magnitude above it.
SAME_IMAGE_MAX_DIFF = 6.0
_COMPARE_SIZE = (64, 64)


def _same_image(a: bytes, b: bytes) -> bool | None:
    """Whether two encoded images are the same picture. None if either won't decode.

    Byte equality is checked first (the common case — the CLI uploads the file as-is), then
    a decoded comparison, so a re-encoded upload is still recognised as ours. Both are
    reduced to a small fixed size before differencing: a re-encode can change dimensions,
    and what matters here is identity of content, not of file."""
    if a == b:
        return True
    try:
        from PIL import Image

        with Image.open(io.BytesIO(a)) as ia, Image.open(io.BytesIO(b)) as ib:
            xa = ia.convert("RGB").resize(_COMPARE_SIZE, Image.BILINEAR)
            xb = ib.convert("RGB").resize(_COMPARE_SIZE, Image.BILINEAR)
        pa, pb = xa.tobytes(), xb.tobytes()
    except Exception:
        return None
    diff = sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)
    return diff <= SAME_IMAGE_MAX_DIFF


def _fetch(url: str, timeout: int = 120) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


class HiggsfieldProvider(ImageProvider):
    name = "higgsfield"
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
        exe = shutil.which(config.HIGGSFIELD_BIN)
        if not exe:
            raise ProviderError(
                f"Higgsfield CLI not found on PATH (looked for '{config.HIGGSFIELD_BIN}'). "
                f"Install it with `npm i -g @higgsfield/cli`, sign in with "
                f"`higgsfield auth login`, and select a workspace if prompted "
                f"(`higgsfield workspace set <id>`)."
            )

        model = model or prefs.get_provider_model("higgsfield") or config.HIGGSFIELD_MODEL
        full_prompt = prompt + (CHROMA_HINT if transparent else "")

        # Before spending anything: did an earlier attempt at THIS exact call already buy
        # the image? A job is billed when Higgsfield accepts it, so a `--wait` timeout, a
        # killed shell or a crash between download and commit all leave a paid-for picture
        # sitting on the account while the step reports failure. Re-running the step then
        # pays for it a second time. The journal remembers the attempt; the account's own
        # job list is where the result actually is.
        refs = [Path(r) for r in (reference_images or []) if Path(r).exists()]
        key = pending.key_for(
            self.name, model, full_prompt, [pending.file_sha(r) for r in refs]
        )
        self.last_recovery = None
        intent = pending.pending(key)
        if intent is not None:
            recovered = self._recover(exe, model, full_prompt, refs, intent, out_path)
            if recovered:
                pending.close_intent(key, job_id=recovered)
                self.last_recovery = recovered
                return out_path
        pending.open_intent(
            key, provider=self.name, model=model, prompt=full_prompt,
            ref=str(refs[0]) if refs else "",
        )

        # `--json` (a documented *global* flag) has to come before the subcommand, not
        # after `--prompt`: with a multi-line prompt — the normal case, since composed
        # prompts are always multiple joined sections — the CLI silently drops a
        # trailing `--json` and prints a human-readable summary instead. Confirmed by
        # reproducing it against `generate cost` (see HiggsfieldProvider.estimate_cost);
        # applying the same fix here since `generate create` takes the identical shape.
        # `--prompt` goes LAST, after every other flag. Same root cause as the `--json`
        # placement note above: a multi-line prompt value makes the CLI stop parsing the
        # flags that follow it. Measured on `generate cost` with the v3 prompt wording
        # (which is deliberately multi-line): a trailing `--quality low` was silently
        # dropped and gpt_image_2 quoted its 7-credit default instead of 0.75 — a 9x
        # overcharge that would have appeared on the bill with nothing in the logs.
        cmd = [
            exe, "--json", "generate", "create", model,
            "--wait", "--wait-timeout", f"{TIMEOUT_S}s",
        ]
        for ref in reference_images or []:
            p = Path(ref)
            if p.exists():
                cmd.extend(["--image-references", str(p)])
        cmd.extend(_param_flags(exe, model, params, reference_images))
        cmd.extend(["--prompt", full_prompt])
        self.last_command = shlex.join(cmd)

        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S + 30,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(f"Higgsfield CLI timed out after {TIMEOUT_S}s")
        except OSError as e:
            raise ProviderError(f"Failed to run Higgsfield CLI: {e}")

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-800:]
            raise ProviderError(f"Higgsfield CLI error (exit {proc.returncode}): {tail}")

        image_url = self._extract_image_url(proc.stdout)
        if not image_url:
            tail = proc.stdout.strip()[-800:]
            raise ProviderError(f"Higgsfield CLI produced no image URL. Output tail:\n{tail}")

        data = _fetch(image_url)
        if data is None:
            raise ProviderError(f"Failed to download Higgsfield image from {image_url}")
        out_path.write_bytes(data)
        # Closed only now, with the bytes on disk: an intent closed at any earlier point
        # would stop a re-run from recovering a job whose image we never actually got.
        # The job id is banked at the same time so no OTHER call's recovery can claim it —
        # every region in a Polish run sends the identical prompt, so a job that has been
        # spent must be visibly spent.
        pending.close_intent(key, job_id=self._extract_job_id(proc.stdout))
        return out_path

    def _recover(
        self, exe: str, model: str, prompt: str, refs: list[Path], intent: dict, out_path: Path
    ) -> str | None:
        """Claim an already-paid job for this call, writing its image to `out_path`.
        Returns the job id, or None if there is nothing safe to claim.

        Safety before recall, throughout: handing back the wrong image would silently put
        one element's artwork on another, which is worse than paying for a second
        generation. So a candidate must be all of —

          * not already banked (`pending.consumed`), so no job is spent twice;
          * created after this call's first attempt began, which is what keeps it inside
            our own run rather than somewhere in the account's history;
          * the same model and byte-identical prompt; and
          * made from the same reference image, verified by downloading the input the job
            actually ran on and comparing it to the local file.

        The last one is what does the real work. Every region in a Polish run shares one
        prompt and one model — the reference is the ONLY thing that distinguishes them —
        so without that check a run of nine elements offers nine indistinguishable
        candidates. When it can't be verified (an input the job list doesn't expose, an
        upload the CLI re-encoded), recovery falls back to claiming only a lone
        unambiguous candidate, and otherwise declines.
        """
        jobs = self._recent_jobs(exe)
        if not jobs:
            return None
        consumed = pending.consumed()
        floor = intent.get("started_at", 0) - RECOVER_CLOCK_SLACK_S
        candidates = [
            j for j in jobs
            if isinstance(j, dict) and j.get("id") and j.get("id") not in consumed
            and (j.get("status") or "").lower() not in DEAD_STATUSES
            and j.get("job_type") == model
            and ((j.get("params") or {}).get("prompt") or "") == prompt
            and _parse_ts(j.get("created_at")) >= floor
        ]
        candidates.sort(key=lambda j: _parse_ts(j.get("created_at")), reverse=True)
        if not candidates:
            return None

        try:
            ref_bytes = refs[0].read_bytes() if refs else b""
        except OSError:
            ref_bytes = b""
        verdicts = [(j, self._same_reference(j, ref_bytes)) for j in candidates[:6]]
        sure = [j for j, v in verdicts if v is True]
        maybe = [j for j, v in verdicts if v is None]
        # Two confirmed matches are two runs of the *same* request (same model, same
        # prompt, same reference bytes), so either image answers this call — take the
        # newest. Unconfirmed ones are only claimable when there is exactly one, since a
        # second could just as easily belong to the element next to this one.
        job = sure[0] if sure else (maybe[0] if len(maybe) == 1 else None)
        if job is None:
            return None

        url = self._result_url(exe, job)
        if not url:
            return None
        data = _fetch(url)
        if data is None:
            return None
        out_path.write_bytes(data)
        return job.get("id")

    def _recent_jobs(self, exe: str) -> list:
        """The account's recent image jobs, newest first. [] on any failure — a recovery
        that can't look is a recovery that doesn't happen, never a failed generation."""
        try:
            proc = subprocess.run(
                [exe, "--json", "generate", "list", "--image", "--size", str(RECOVER_LIST_SIZE)],
                capture_output=True, text=True, timeout=LIST_TIMEOUT_S,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                return []
            payload = json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("items", "jobs", "data", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    def _same_reference(self, job: dict, ref_bytes: bytes) -> bool | None:
        """Did this job run on the reference image we are about to send? True/False are
        verdicts; None means "couldn't tell" (the job list exposes no input, or the upload
        wouldn't download or decode) and leaves the caller to fall back on uniqueness."""
        inputs = (job.get("params") or {}).get("input_images") or []
        urls = [i.get("url") for i in inputs if isinstance(i, dict) and i.get("url")]
        if not urls or not ref_bytes:
            return None
        data = _fetch(urls[0], timeout=30)
        if data is None:
            return None
        return _same_image(data, ref_bytes)

    def _result_url(self, exe: str, job: dict) -> str | None:
        """The finished image's URL, waiting for the job first if it is still running.

        A job that is still in flight is the *most* recoverable case there is — it is the
        one the `--wait` timeout gave up on while Higgsfield kept working, and it is
        already paid for — so it is worth the wait rather than being written off.
        """
        if job.get("result_url"):
            return job["result_url"]
        try:
            proc = subprocess.run(
                [exe, "--json", "generate", "wait", str(job.get("id")), "--timeout", f"{TIMEOUT_S}s", "-q"],
                capture_output=True, text=True, timeout=TIMEOUT_S + 30,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        return self._extract_image_url(proc.stdout)

    def estimate_cost(
        self,
        prompt: str,
        model: str | None = None,
        reference_images: list[Path] | None = None,
        transparent: bool = True,
        params: dict | None = None,
    ) -> float | None:
        """Real credit cost from Higgsfield's own `generate cost` — no job created,
        params matching `generate create` (same prompt/reference-image shape). This
        is billing truth, not a guess: unlike a generic per-character token estimate,
        it reflects the actual per-model pricing the account will be charged.

        Best-effort: None on anything short of a clean parse (CLI missing, not signed
        in, a network hiccup) so a failed estimate never blocks Generate — it's a
        preview, not a precondition.
        """
        exe = shutil.which(config.HIGGSFIELD_BIN)
        if not exe:
            return None
        model = model or prefs.get_provider_model("higgsfield") or config.HIGGSFIELD_MODEL
        full_prompt = prompt + (CHROMA_HINT if transparent else "")
        # `--json` must lead (see the note on `generate`'s own `cmd` above) — this is
        # the case that surfaced the bug: `generate cost` prints "7 credits\n" instead
        # of `{"credits": 7}` when `--json` trails a multi-line `--prompt`.
        cmd = [exe, "--json", "generate", "cost", model]
        for ref in reference_images or []:
            p = Path(ref)
            if p.exists():
                cmd.extend(["--image-references", str(p)])
        # Same params as the real call, or the quote is for a different job: measured live,
        # `nano_banana_flash` is 1.5 credits at its default 1k but 3 at `--resolution 4k`,
        # and `gpt_image_2` is 7 by default but 0.75 at `--quality low`.
        cmd.extend(_param_flags(exe, model, params, reference_images))
        # ...and `--prompt` last, for the flag-swallowing reason documented in `generate`.
        cmd.extend(["--prompt", full_prompt])
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=COST_TIMEOUT_S,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        stdout = proc.stdout.strip()
        if not stdout:
            return None
        # `--json` pretty-prints (`{\n  "credits": 7\n}`), so this must parse the whole
        # blob — not just its last line, which on its own is only the closing brace.
        try:
            payload = json.loads(stdout)
            credits = payload.get("credits") if isinstance(payload, dict) else None
            if isinstance(credits, (int, float)):
                return float(credits)
        except json.JSONDecodeError:
            pass
        # Defensive fallback for the plain-text shape ("7 credits", "0.12 credits") in
        # case some CLI build still emits it despite the leading `--json`.
        last_line = stdout.splitlines()[-1].strip()
        m = re.match(r"^([\d.]+)\s*credits?\b", last_line, re.IGNORECASE)
        return float(m.group(1)) if m else None

    @staticmethod
    def _payload(stdout: str):
        """The CLI's `--json` result, parsed defensively: the last valid JSON value on
        stdout (some CLIs emit progress lines before the final result), else the whole
        blob (which `--json` pretty-prints across several lines). None if neither parses."""
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return None

    @classmethod
    def _extract_job_id(cls, stdout: str) -> str | None:
        """The finished job's own id, so it can be banked as spent (see
        `pending.close_intent`). Best-effort: an id we fail to record only costs a later
        recovery some certainty — it never affects the image just produced."""
        payload = cls._payload(stdout)
        if isinstance(payload, list):
            payload = payload[0] if payload and isinstance(payload[0], dict) else None
        if not isinstance(payload, dict):
            return None
        for k in ("id", "job_id", "generation_id"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
        job = payload.get("job")
        if isinstance(job, dict) and isinstance(job.get("id"), str):
            return job["id"]
        return None

    @classmethod
    def _extract_image_url(cls, stdout: str) -> str | None:
        """Walk the CLI's result for anything that looks like a media URL, preferring keys
        named url/image_url/output(_url)."""
        payload = cls._payload(stdout)
        if payload is None:
            return None

        preferred_keys = ("url", "image_url", "output_url", "result_url")
        fallback: str | None = None
        url_re = re.compile(r"^https?://\S+\.(png|jpe?g|webp)(\?\S*)?$", re.IGNORECASE)

        def walk(node) -> str | None:
            nonlocal fallback
            if isinstance(node, dict):
                for k in preferred_keys:
                    v = node.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
                for v in node.values():
                    found = walk(v)
                    if found:
                        return found
            elif isinstance(node, list):
                for v in node:
                    found = walk(v)
                    if found:
                        return found
            elif isinstance(node, str) and node.startswith("http"):
                if url_re.match(node):
                    return node
                if fallback is None:
                    fallback = node
            return None

        return walk(payload) or fallback
