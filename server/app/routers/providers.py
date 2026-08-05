"""Settings ("Providers") API.

One place to see every image + text provider: what it is, how it's used, whether
it costs money, whether it's configured/available, and whether it's enabled — plus
the toggles to control all of that so the user never spends on a path they didn't
choose.

- GET  /api/providers            → full status for the Settings page
- PUT  /api/providers/settings   → update enable/disable, default, gemini API toggle
"""
from fastapi import APIRouter
from pydantic import BaseModel

from ..llm.registry import providers_info as llm_providers_info
from ..providers import get_provider, prefs
from ..providers.registry import (
    image_model_params,
    image_providers_info,
    resolve_model,
    resolve_params,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("")
def get_providers():
    p = prefs.load()
    return {
        "image": image_providers_info(),
        "text_llm": llm_providers_info(),
        "settings": {
            "default_image_provider": p["default_image_provider"],
        },
    }


@router.get("/{provider_name}/models/{model}/params")
def get_model_params(provider_name: str, model: str):
    """Tunable knobs for one model, so the picker can offer exactly the options that model
    accepts (`quality: low|medium|high` on GPT Image, `resolution: 1k|2k|4k` on Nano Banana,
    neither on FLUX Kontext). Per-model and on demand — see `image_model_params`."""
    return {"params": image_model_params(provider_name, model)}


@router.get("/{provider_name}/models/{model}/estimate-cost")
def get_per_call_cost(provider_name: str, model: str):
    """Per-generation credit cost for a model at its currently saved params —
    Higgsfield's own `generate cost`, not tied to any one asset's prompt (pricing is
    driven by model + quality/resolution, not prompt text; see `HiggsfieldProvider.
    estimate_cost`). Lets a step that's about to run N provider calls (Polish, Text)
    show `N * this` without pricing every asset individually. `supported: False` for a
    flat-subscription provider like Antigravity, which has nothing to estimate."""
    if provider_name != "higgsfield":
        return {"supported": False, "credits": None}
    resolved_model = resolve_model(provider_name, model)
    hf = get_provider(provider_name)
    credits = hf.estimate_cost(
        "", model=resolved_model, reference_images=None,
        params=resolve_params(provider_name, resolved_model),
    )
    return {"supported": True, "credits": credits}


class ProviderSettingsPatch(BaseModel):
    enabled: dict[str, bool] | None = None
    default_image_provider: str | None = None
    provider_models: dict[str, str] | None = None
    # Was missing, so every `provider_visual_models` write the client made was silently
    # discarded by Pydantic as an undeclared field — the Antigravity visual-model dropdown
    # appeared to save and never did.
    provider_visual_models: dict[str, str] | None = None
    # provider -> model -> {param: value}; see prefs.DEFAULTS["provider_params"].
    provider_params: dict[str, dict[str, dict]] | None = None


@router.put("/settings")
def put_providers(body: ProviderSettingsPatch):
    patch = body.model_dump(exclude_none=True)
    prefs.save(patch)
    return get_providers()
