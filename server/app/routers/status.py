"""Availability checks shown in the sidebar status card.

- Antigravity: `antigravity` CLI on PATH (used for both images and text)
- Higgsfield: `higgsfield` CLI on PATH + signed in
- LLM CLIs: claude / antigravity on PATH
Results are cached for the process lifetime; POST /api/status/refresh re-probes.
"""
import shutil

from fastapi import APIRouter

from .. import config
from ..providers import prefs
from ..providers.registry import higgsfield_status

router = APIRouter(prefix="/api/status", tags=["status"])

_cache: dict | None = None


def _antigravity_status() -> dict:
    exe = shutil.which(config.ANTIGRAVITY_BIN)
    return {
        "ok": bool(exe),
        "detail": f"CLI found ({exe})" if exe else f"'{config.ANTIGRAVITY_BIN}' CLI not on PATH",
    }


def _higgsfield_status_for_sidebar() -> dict:
    # providers.registry's probe uses "configured" (paired with a richer detail
    # string); the sidebar's StatusInfo shape uses "ok" — translate here rather than
    # making two call sites agree on field names.
    status = higgsfield_status()
    return {"ok": bool(status.get("configured")), "detail": status.get("detail", "")}


def probe() -> dict:
    from .. import ml

    claude = shutil.which("claude")
    antigravity = shutil.which(config.ANTIGRAVITY_BIN)
    enabled = prefs.load()["enabled"]
    return {
        # Local extraction/repair stack. When this reports torch=false the pipeline still
        # runs, on classical fallbacks — surfaced so a quality drop is never silent.
        "ml": ml.status(),
        "antigravity": _antigravity_status(),
        "higgsfield": _higgsfield_status_for_sidebar(),
        "enabled": {k: bool(enabled.get(k, True)) for k in ("antigravity", "higgsfield")},
        "llm_clis": {
            "claude": {"ok": bool(claude), "path": claude or ""},
            "antigravity": {"ok": bool(antigravity), "path": antigravity or ""},
        },
    }


@router.get("")
def get_status():
    global _cache
    if _cache is None:
        _cache = probe()
    return _cache


@router.post("/refresh")
def refresh_status():
    global _cache
    _cache = probe()
    return _cache
