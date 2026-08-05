"""Persisted provider preferences — the spend guard.

A single `AppSetting` row (`provider_prefs`) stores which image providers are
enabled and which is the default, plus whether the (paid) Gemini REST API path
may be used. Disabling a provider here makes the server *refuse* to generate with
it, so a pasted API key can never quietly cost money once its provider is off.

Read paths are cached in-process for speed; every write refreshes the cache, so
toggling in Settings takes effect immediately without a server restart.
"""
from ..db import SessionLocal
from ..models import AppSetting

_KEY = "provider_prefs"

# Image providers, keyed by the name `get_provider()` understands.
IMAGE_PROVIDERS = ("antigravity", "higgsfield")

DEFAULTS: dict = {
    # All enabled by default; the Settings page lets the user turn a paid path
    # (Higgsfield) OFF so it can never spend money unless deliberately chosen.
    "enabled": {"antigravity": True, "higgsfield": True},
    "default_image_provider": "higgsfield",
    "provider_models": {
        "antigravity": "gemini-3.6-flash-high",
        # Chosen by measurement, not by list order — see tools/sweep_polish_text.py. Over a
        # 6-model sweep on ClashUp's screen, gpt_image_2 at `--quality low` scored highest
        # on text removal (81.7 vs the previous default nano_banana_flash's 78.8) at HALF
        # the price (0.75 vs 1.50 credits/call). Its 7-credit headline is the `high` tier;
        # the `low` tier below is what makes it the cheapest good option rather than the
        # most expensive one.
        "higgsfield": "gpt_image_2",
    },
    "provider_visual_models": {
        "antigravity": "auto",
    },
    # Per-model generation knobs, keyed provider -> model -> {param: value}. Keyed by MODEL
    # because the accepted set differs per model (Seedream takes `quality`, FLUX takes
    # `resolution`, Nano Banana takes neither) — a flat per-provider dict would carry an
    # invalid flag across a model switch, and the CLI rejects flags a model does not
    # declare.
    "provider_params": {
        "higgsfield": {
            # `auto` is resolved at call time from the reference image's real proportions
            # (HiggsfieldProvider.AUTO_ASPECT). Every model defaults to a 1:1 canvas, so a
            # 2.14:1 button came back drawn small on a square and had to be trimmed and
            # padded back — measured at SSIM 0.89 -> 0.31. Asking for the right shape is
            # free at every tier tested.
            "gpt_image_2": {"aspect_ratio": "auto", "quality": "low"},
        },
    },
}

_cache: dict | None = None


def _merge(base: dict, patch: dict) -> dict:
    out = {**base, **patch}
    if "enabled" in patch:
        out["enabled"] = {**base.get("enabled", {}), **patch["enabled"]}
    if "provider_models" in patch:
        out["provider_models"] = {**base.get("provider_models", {}), **patch["provider_models"]}
    if "provider_visual_models" in patch:
        out["provider_visual_models"] = {**base.get("provider_visual_models", {}), **patch["provider_visual_models"]}
    if "provider_params" in patch:
        # Merged two levels deep (provider, then model) so saving one model's `quality`
        # doesn't wipe another model's saved `resolution`. The innermost dict is replaced
        # wholesale, which is what lets a param be *cleared* by sending the model's dict
        # without it — needed when switching to a model that doesn't accept that flag.
        merged = {**base.get("provider_params", {})}
        for provider, models in patch["provider_params"].items():
            merged[provider] = {**merged.get(provider, {}), **(models or {})}
        out["provider_params"] = merged
    return out


def load() -> dict:
    global _cache
    if _cache is None:
        with SessionLocal() as db:
            row = db.get(AppSetting, _KEY)
            _cache = _merge(DEFAULTS, row.value if row else {})
    return _cache


def save(patch: dict) -> dict:
    global _cache
    merged = _merge(load(), patch)
    with SessionLocal() as db:
        row = db.get(AppSetting, _KEY)
        if not row:
            row = AppSetting(key=_KEY, value={})
            db.add(row)
        row.value = merged
        db.commit()
    _cache = merged
    return merged


def is_enabled(name: str) -> bool:
    return bool(load()["enabled"].get(name, True))


def default_image_provider() -> str:
    return load()["default_image_provider"]


def get_provider_model(name: str) -> str:
    models = load().get("provider_models", {})
    return models.get(name, DEFAULTS["provider_models"].get(name, ""))


def get_provider_visual_model(name: str) -> str:
    models = load().get("provider_visual_models", {})
    return models.get(name, DEFAULTS.get("provider_visual_models", {}).get(name, "auto"))


def get_provider_params(name: str, model: str | None = None) -> dict:
    """Saved per-model generation knobs, or {} if none. Falls back to the shipped defaults
    for a model the user has never touched, so the measured recommendation applies out of
    the box rather than only after someone opens the dropdown."""
    if not model:
        return {}
    saved = (load().get("provider_params", {}) or {}).get(name, {})
    if model in saved:
        return dict(saved[model] or {})
    return dict((DEFAULTS.get("provider_params", {}).get(name, {}) or {}).get(model, {}))
