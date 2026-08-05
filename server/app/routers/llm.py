from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..llm.registry import providers_info
from ..llm.runner import LlmError, LlmOptions, get_runner
from ..models import AppSetting
from ..prompting import compose_sections
from .assets import get_asset_or_404
from .projects import get_project_or_404

router = APIRouter(prefix="/api/llm", tags=["llm"])

DEFAULTS = {"provider": "claude", "model": "", "effort": "medium", "context_window": None}


class LlmSelection(BaseModel):
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    context_window: int | None = None


def get_llm_defaults(db: Session) -> dict:
    row = db.get(AppSetting, "llm_defaults")
    return {**DEFAULTS, **(row.value if row else {})}


def resolve_options(db: Session, sel: LlmSelection | None) -> tuple[str, LlmOptions]:
    from ..llm.runner import RUNNERS
    defaults = get_llm_defaults(db)
    provider = (sel.provider if sel and sel.provider else defaults["provider"])
    # A previously-saved default may name a removed provider (e.g. "gemini");
    # fall back to the built-in default rather than raising LlmError later.
    if provider not in RUNNERS:
        provider = DEFAULTS["provider"]
    options = LlmOptions(
        model=(sel.model if sel and sel.model is not None else defaults["model"]) or "",
        effort=(sel.effort if sel and sel.effort is not None else defaults["effort"]) or "",
        context_window=sel.context_window if sel and sel.context_window else defaults["context_window"],
    )
    return provider, options


@router.get("/providers")
def list_providers():
    return providers_info()


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return get_llm_defaults(db)


@router.put("/settings")
def put_settings(body: LlmSelection, db: Session = Depends(get_db)):
    row = db.get(AppSetting, "llm_defaults")
    if not row:
        row = AppSetting(key="llm_defaults", value={})
        db.add(row)
    merged = {**get_llm_defaults(db), **body.model_dump(exclude_unset=True)}
    row.value = merged
    db.commit()
    return merged


class RefineBody(BaseModel):
    llm: LlmSelection | None = None


@router.post("/assets/{asset_id}/refine-prompt")
def refine_prompt(asset_id: int, body: RefineBody, db: Session = Depends(get_db)):
    """Rewrite the asset's user prompt into a stronger image prompt, honoring the
    project art style. Returns the suggestion without saving it."""
    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    sections = compose_sections(
        project, asset.type, asset.prompt, asset.aspect_ratio, asset.resolution,
        is_sliced=(asset.nine_slice is not None),
    )

    provider, options = resolve_options(db, body.llm)
    runner = get_runner(provider)
    instruction = (
        "Task: rewrite an image-generation prompt for a 2D game asset. "
        "This is a one-shot text task — do not ask questions, do not use tools, "
        "do not explain.\n"
        f"Project art style: {sections['style'] or '(none defined)'}\n"
        f"Asset type rules (already enforced separately, do not repeat them): {sections['rules']}\n"
        f"Prompt to rewrite: {sections['user'] or '(empty — invent a fitting prompt for the asset name)'}\n"
        f"Asset name: {asset.name}\n\n"
        "Rewrite it to be more specific and visually descriptive while keeping the "
        "intent. Describe shape, materials, lighting and mood. Do not mention "
        "transparency, backgrounds, file formats or slicing. Under 60 words. "
        "Your entire reply must be the rewritten prompt text and nothing else."
    )
    try:
        suggestion = runner.run(instruction, options)
    except LlmError as e:
        raise HTTPException(502, str(e))
    return {"prompt": suggestion.strip().strip('"')}
