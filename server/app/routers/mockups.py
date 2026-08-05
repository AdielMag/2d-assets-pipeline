import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from PIL import Image, ImageOps
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import layout
from ..db import SessionLocal, get_db
from ..llm.runner import LlmError, LlmOptions, get_runner
from ..models import Asset, AssetVersion, Atlas, Mockup, MockupLabel, MockupRegion, Project
from ..processing.composite import composite_layout
from ..processing.extract import extract_region
from ..processing.fidelity import fit_mode_for
from ..processing.inpaint import (
    alpha_to_mask, boxes_to_mask, deocclude, dilate_mask, plausible_fill,
)
from ..processing.nine_slice import detect_borders, detect_borders_if_frame
from ..processing.region_detector import (
    containment_ratio, filter_and_refine_regions, hamming, perceptual_hash,
)
from ..processing.subdivide import (
    has_children, is_already_covered, repeat_units, to_screen_pct,
)
from ..processing.reference import letterbox_reference, snap_ratio
from ..processing.transparency import remove_background
from ..processing.trim import fit_to_resolution, parse_resolution, trim_for_fit
from ..progress import Emitter, NoEmit, estimate_tokens, get_stored_runs, is_quota_error, make_emitter, sse_response
from ..prompting import (
    CHROMA_HINT, build_text_removal_instruction, compose_prompt, extraction_op_keys,
    ratio_string, reference_instruction,
)
from ..providers import ProviderError, get_enabled_provider, resolve_model
from ..providers.registry import (
    extraction_model_warning,
    model_aspect_options,
    resolve_extraction_model,
    resolve_params,
)
from ..schemas import (
    ASSET_TYPES,
    DEFAULT_RESOLUTION,
    AssetOut,
    LabelOut,
    LabelUpdate,
    MockupOut,
    MockupUpdate,
    RegionCreate,
    RegionOut,
    RegionUpdate,
)
from ..storage import (
    abs_path, new_asset_path, new_image_path, new_mockup_path, new_work_path, rel_path, slugify,
)
from ..unity_export import (
    ExportError,
    export_asset,
    export_atlases,
    export_screen_layout,
    export_screen_reference,
    screen_element,
)
from .atlases import available_assets, get_atlas_or_404
from .llm import LlmSelection, resolve_options
from .projects import get_project_or_404

router = APIRouter(prefix="/api", tags=["mockups"])


@router.get("/mockups/{mockup_id}/generations")
def get_mockup_generations(mockup_id: int, db: Session = Depends(get_db)):
    mockup = get_mockup_or_404(db, mockup_id)
    return get_stored_runs(mockup.project_id, entity_id=mockup.id, entity_type="mockup")



def get_mockup_or_404(db: Session, mockup_id: int) -> Mockup:
    mockup = db.get(Mockup, mockup_id)
    if not mockup:
        raise HTTPException(404, "Mockup not found")
    return mockup


def get_region_or_404(db: Session, region_id: int) -> MockupRegion:
    region = db.get(MockupRegion, region_id)
    if not region:
        raise HTTPException(404, "Region not found")
    return region


def get_label_or_404(db: Session, label_id: int) -> MockupLabel:
    label = db.get(MockupLabel, label_id)
    if not label:
        raise HTTPException(404, "Label not found")
    return label


@router.get("/projects/{project_id}/mockups", response_model=list[MockupOut])
def list_mockups(project_id: int, db: Session = Depends(get_db)):
    get_project_or_404(db, project_id)
    return db.scalars(
        select(Mockup).where(Mockup.project_id == project_id).order_by(Mockup.created_at.desc())
    ).all()


@router.post("/projects/{project_id}/mockups/upload", response_model=MockupOut)
async def upload_mockup(project_id: int, file: UploadFile, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    ext = (file.filename or "mockup.png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, "Unsupported image type")
    # The row comes first because its id names the folder the screenshot goes in — which
    # is what lets deleting the mockup delete the screenshot and every crop taken from it.
    mockup = Mockup(project_id=project.id, image_path="", prompt="")
    db.add(mockup)
    db.commit()
    dest = new_mockup_path(project.id, mockup.id, "mockup", ext)
    dest.write_bytes(await file.read())
    mockup.image_path = rel_path(dest)
    db.commit()
    return mockup


class MockupGenerateBody(BaseModel):
    prompt: str
    provider: str = "antigravity"
    model: str | None = None
    visual_model: str | None = None


@router.post("/projects/{project_id}/mockups/generate", response_model=MockupOut)
def generate_mockup(project_id: int, body: MockupGenerateBody, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    try:
        provider = get_enabled_provider(body.provider)
    except ProviderError as e:
        raise HTTPException(403, str(e))
    palette = ", ".join(project.palette) if project.palette else ""
    prompt = "\n".join(
        p for p in [
            f"Art style: {project.style_description}" if project.style_description else "",
            f"Color palette: {palette}" if palette else "",
            "Full game screen mockup, complete UI composition filling the whole canvas.",
            body.prompt,
        ] if p
    )
    # As in `upload_mockup`: the row is created first so the image can be written into the
    # mockup's own folder. A generation that fails takes the empty row back out with it.
    mockup = Mockup(project_id=project.id, image_path="", prompt=body.prompt)
    db.add(mockup)
    db.commit()
    dest = new_mockup_path(project.id, mockup.id, "mockup")
    try:
        model = resolve_model(body.provider, body.model)
        provider.generate(
            prompt, dest, size="1536x1024", transparent=False,
            model=model, visual_model=getattr(body, "visual_model", None),
        )
    except ProviderError as e:
        db.delete(mockup)
        db.commit()
        layout.remove_dir(layout.mockup_dir(project.id, mockup.id))
        raise HTTPException(502, str(e))
    mockup.image_path = rel_path(dest)
    db.commit()
    return mockup


@router.patch("/mockups/{mockup_id}", response_model=MockupOut)
def update_mockup(mockup_id: int, body: MockupUpdate, db: Session = Depends(get_db)):
    mockup = get_mockup_or_404(db, mockup_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(mockup, field, value)
    db.commit()
    return mockup


@router.delete("/mockups/{mockup_id}")
def delete_mockup(mockup_id: int, db: Session = Depends(get_db)):
    mockup = get_mockup_or_404(db, mockup_id)
    project_id = mockup.project_id
    db.delete(mockup)
    db.commit()
    # The screenshot and every crop taken off it live in the mockup's own folder. Assets
    # built from those crops are NOT in there — they belong to their domain — so deleting
    # a screen never takes its extracted art with it.
    layout.remove_dir(layout.mockup_dir(project_id, mockup_id))
    return {"ok": True}


# Target device width the exported assets are sized for. Resolutions are derived from
# each region's *fraction of the screen* against this width (not the mockup's own pixel
# size, which is often a small screenshot), so an element is never authored smaller than
# the footprint it fills on a high-DPI phone. Undersizing here is what makes assets look
# pixelated once Unity's point filter upscales them to their on-screen size.
REFERENCE_DEVICE_WIDTH = 1440
MIN_RESOLUTION_DIM = 128    # floor on the LONG edge; icons never drop below this
MAX_RESOLUTION_DIM = 2048   # sanity cap


def calculate_region_resolution(mockup_img_w: int, mockup_img_h: int, region_w_pct: float, region_h_pct: float) -> str:
    """Optimal target pixel resolution for a region, from its fraction of the screen scaled
    to REFERENCE_DEVICE_WIDTH. Preserves the region's true aspect ratio (so wide buttons
    aren't squeezed into near-square boxes) and floors the long edge at MIN_RESOLUTION_DIM."""
    aspect = (mockup_img_h / mockup_img_w) if mockup_img_w else 1.0
    device_w = REFERENCE_DEVICE_WIDTH
    device_h = REFERENCE_DEVICE_WIDTH * aspect
    px_w = max(1.0, device_w * (region_w_pct / 100.0))
    px_h = max(1.0, device_h * (region_h_pct / 100.0))
    # scale up so the long edge meets the floor, keeping aspect
    long_edge = max(px_w, px_h)
    if long_edge < MIN_RESOLUTION_DIM:
        s = MIN_RESOLUTION_DIM / long_edge
        px_w, px_h = px_w * s, px_h * s
    res_w = min(MAX_RESOLUTION_DIM, max(16, ((round(px_w) + 8) // 16) * 16))
    res_h = min(MAX_RESOLUTION_DIM, max(16, ((round(px_h) + 8) // 16) * 16))
    return f"{res_w}x{res_h}"


@router.post("/mockups/{mockup_id}/regions", response_model=RegionOut)
def create_region(mockup_id: int, body: RegionCreate, db: Session = Depends(get_db)):
    mockup = get_mockup_or_404(db, mockup_id)
    if body.asset_type not in ASSET_TYPES:
        raise HTTPException(400, f"asset_type must be one of {ASSET_TYPES}")
    
    res = body.resolution
    if not res:
        src = abs_path(mockup.image_path)
        if src.exists():
            with Image.open(src) as img:
                res = calculate_region_resolution(img.width, img.height, body.w, body.h)
        else:
            res = DEFAULT_RESOLUTION

    data = body.model_dump()
    data["resolution"] = res
    region = MockupRegion(mockup_id=mockup_id, detect_rect=[body.x, body.y, body.w, body.h], **data)
    db.add(region)
    db.commit()
    return region


@router.patch("/regions/{region_id}", response_model=RegionOut)
def update_region(region_id: int, body: RegionUpdate, db: Session = Depends(get_db)):
    region = get_region_or_404(db, region_id)
    old_box = (region.x, region.y, region.w, region.h)
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(region, field, value)
    new_box = (region.x, region.y, region.w, region.h)
    if old_box != new_box or region.detect_rect is None:
        region.detect_rect = [region.x, region.y, region.w, region.h]
    if old_box != new_box and region.asset_id is not None and "asset_id" not in updates:
        # An already-built region whose box just moved is bound to an asset cut from the
        # OLD rect — reusing it silently would keep showing the stale crop. Unbinding
        # forces the next build to actually re-extract it; see the model's force_rebuild
        # comment for why this is "unbound by an edit", not "never built".
        #
        # A text-choice change (a caption's text_mode, PATCH /labels/{id}) does NOT
        # unbind: the Text step runs its redraw directly against whatever asset is
        # currently bound, whenever the user asks it to — it never needs a fresh CV
        # extraction first.
        region.asset_id = None
        region.force_rebuild = True
    db.commit()
    return region


@router.delete("/regions/{region_id}")
def delete_region(region_id: int, db: Session = Depends(get_db)):
    region = get_region_or_404(db, region_id)
    db.delete(region)
    db.commit()
    return {"ok": True}


@router.patch("/labels/{label_id}", response_model=LabelOut)
def update_label(label_id: int, body: LabelUpdate, db: Session = Depends(get_db)):
    """Saves this caption's own Keep/Remove/Extract choice for the Text step — see
    `MockupLabel.text_mode`. A caption never drives `asset_id`/`force_rebuild` the way a
    region's box does, so there is no unbind logic to mirror from `update_region`."""
    label = get_label_or_404(db, label_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(label, field, value)
    db.commit()
    return label


def _delete_asset_files(db: Session, asset: Asset) -> None:
    """Remove an asset's folder. Call BEFORE `db.delete(asset)` — the folder is derived
    from the asset's name and its domain chain, which are gone once the row is."""
    layout.remove_dir(layout.asset_dir(db, asset))


def _crop_region(db: Session, region: MockupRegion, asset: Asset | None = None) -> str:
    """Crop the region out of its mockup image; returns storage-relative path.

    Filed under `asset` when the crop is being kept as that asset's provenance, and under
    the mockup otherwise (the Regions tab's on-demand preview crop) — either way it lands
    with whatever owns it, so it is moved and deleted along with it."""
    mockup = get_mockup_or_404(db, region.mockup_id)
    src = abs_path(mockup.image_path)
    if not src.exists():
        raise HTTPException(400, "Mockup image file missing")
    with Image.open(src) as img:
        w, h = img.size
        box = (
            int(w * region.x / 100),
            int(h * region.y / 100),
            int(w * (region.x + region.w) / 100),
            int(h * (region.y + region.h) / 100),
        )
        crop = img.crop(box)
        dest = (
            new_asset_path(db, asset, region.name) if asset is not None
            else new_mockup_path(mockup.project_id, mockup.id, region.name)
        )
        crop.save(dest)
    return rel_path(dest)


@router.get("/regions/{region_id}/crop")
def region_crop(region_id: int, db: Session = Depends(get_db)):
    region = get_region_or_404(db, region_id)
    return {"path": _crop_region(db, region)}


class DraftBody(BaseModel):
    llm: LlmSelection | None = None


@router.post("/regions/{region_id}/draft-prompt", response_model=RegionOut)
def draft_region_prompt(region_id: int, body: DraftBody, db: Session = Depends(get_db)):
    """Vision-draft an isolation prompt for the region using the chosen LLM CLI."""
    region = get_region_or_404(db, region_id)
    crop_rel = _crop_region(db, region)
    crop_abs = abs_path(crop_rel)

    provider, options = resolve_options(db, body.llm)
    runner = get_runner(provider)
    options.images = [crop_abs]
    instruction = (
        f"Look at the image file at this path: {crop_abs}\n"
        f"It is a cropped reference image named '{region.name}' from a game screen mockup, "
        f"intended to become a standalone {region.asset_type} sprite.\n"
        "Task: write an image-generation prompt for an AI model to extract this element from the reference image. "
        "The prompt must tell the AI to take the reference crop and extract the element exactly as shown, "
        "preserving its exact artwork, shapes, colors, and details, isolated without its background. "
        "Incorporate a precise description of the element's visual shapes, materials, colors, and key details. "
        "Do not mention file formats or slicing. Under 60 words. "
        "Your entire reply must be the prompt text and nothing else."
    )
    try:
        draft = runner.run(instruction, options)
    except LlmError:
        # template fallback when no LLM CLI is available
        draft = (
            f"Take the cropped reference image of '{region.name}' and extract the element from it exactly as shown, "
            f"preserving its exact shapes, artwork, details, and colors, isolated without its original background."
        )
    region.prompt = draft.strip().strip('"')
    db.commit()
    return region


class RegionGenerateBody(BaseModel):
    provider: str  # antigravity | higgsfield
    atlas_id: int | None = None  # file a newly-created asset into this domain
    resolution: str | None = None  # target resolution for a newly-generated asset


class RegionExtractBody(BaseModel):
    atlas_id: int | None = None
    resolution: str | None = None
    method: str = "auto"  # auto | classical | sam2


_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Generic widget-kind words that name what a control IS, not which family it belongs to.
# Two elements are the same family only if they share a *meaningful* token beyond these —
# GemCurrencyPill and GoldCurrencyPill are siblings via "currency", but PlayButton and
# GameModeButton share only "button" and are intentionally different-looking, so they must
# NOT anchor to each other's frame.
_GENERIC_TOKENS = {
    "button", "btn", "panel", "bar", "frame", "icon", "display", "hud", "pill",
    "badge", "plate", "container", "box", "bg", "background", "ui", "element",
    "widget", "slot", "tile", "card", "window", "popup", "modal", "menu",
    "nav", "navbar", "navigation", "tab", "item",
}


def _name_tokens(name: str) -> set[str]:
    """Lowercase word tokens of an asset name, splitting PascalCase/camelCase, digits and
    separators, for spotting same-family siblings. GemCurrencyDisplay and
    GoldCurrencyDisplay share {currency, display}; PlayButton / SpinButton share {button}."""
    return {t.lower() for t in _TOKEN_RE.findall(name) if t}


def _family_tokens(name: str) -> set[str]:
    """Name tokens that identify the family (generic widget-kind words removed), used to
    decide whether two assets are the same repeated component that should share a frame."""
    return _name_tokens(name) - _GENERIC_TOKENS


def _project_refs(project: Project) -> list:
    """The project's uploaded style-anchor images (up to 2), as existing absolute paths."""
    out = []
    for rel in (project.reference_images or [])[:2]:
        p = abs_path(rel)
        if p.exists():
            out.append(p)
    return out


def _selected_version_path(asset: Asset):
    """Absolute path of an asset's selected processed image, or None."""
    version = next((v for v in asset.versions if v.id == asset.selected_version_id), None)
    return abs_path(version.processed_path) if version else None


def _style_references(db: Session, project: Project, asset: Asset, atlas_id: int | None):
    """Extra reference images that keep a region-generated asset on-model:
    - the project's uploaded style-anchor images (up to 2) so every asset shares one look;
    - a same-family sibling already generated in this domain (same asset type sharing a
      name token) so the model reproduces its frame/border/material and only swaps the
      inner icon or label.
    Returns (style_ref_paths, sibling_path_or_None)."""
    style_refs = _project_refs(project)

    sibling = None
    aid = asset.atlas_id or atlas_id
    if aid is not None:
        atlas = db.get(Atlas, aid)
        if atlas is not None:
            tokens = _family_tokens(asset.name)
            for cand in available_assets(db, atlas):
                if cand.id == asset.id or cand.type != asset.type:
                    continue
                # Same family only if they share a meaningful (non-generic) token. Without a
                # family token (e.g. a bare "PlayButton"), don't anchor to anything — a shared
                # "button"/"panel" alone is not a reason to copy another element's frame.
                if not tokens or not (tokens & _family_tokens(cand.name)):
                    continue
                version = next(
                    (v for v in cand.versions if v.id == cand.selected_version_id), None
                )
                if version and abs_path(version.processed_path).exists():
                    sibling = abs_path(version.processed_path)
                    break
    return style_refs, sibling


def _ensure_region_asset(
    db: Session, region: MockupRegion, project: Project, target_res: str,
    atlas_id: int | None, resolution: str | None, source: str,
) -> Asset:
    """The Asset row a region's output belongs to — reused if the region already has one,
    created otherwise. Shared by the extraction and generation paths so both file into
    the same domain with the same aspect/resolution/9-slice seeding."""
    asset = db.get(Asset, region.asset_id) if region.asset_id else None
    if asset is not None:
        asset.prompt = region.prompt or asset.prompt
        asset.source = source
        if resolution:
            asset.resolution = resolution
        db.commit()
        return asset

    asset = Asset(
        project_id=project.id,
        atlas_id=atlas_id,
        name=region.name,
        type=region.asset_type,
        prompt=region.prompt,
        source=source,
        # the region's own rect is the ground truth for the shape this asset must
        # fill — bake it in now so it's never lost, in-tool generation and the
        # copy-pasted external prompt both honor it.
        aspect_ratio=ratio_string(region.w, region.h),
        resolution=target_res,
        # ui_elements are 9-sliced (resizable buttons/panels/frames); seed default
        # guides so detect_borders refines them and the trim step keeps a tight fill.
        # Matches create_asset; without it region-generated buttons never got sliced.
        nine_slice={"l": 32, "t": 32, "r": 32, "b": 32} if region.asset_type == "ui_element" else None,
    )
    db.add(asset)
    db.commit()
    region.asset_id = asset.id
    db.commit()
    return asset


def _finalize_version(
    db: Session, asset: Asset, out: Image.Image, *, provider: str, model: str | None,
    prompt: str, raw_rel: str, region: MockupRegion | None = None, emit=None,
    box_aligned: bool = False, reference_paths: list[str] | None = None,
) -> AssetVersion:
    """Post-process a produced image and record it as the asset's selected version.

    The one place the trim → fit → save → version → 9-slice → score sequence lives, so
    extraction and generation can never drift apart in how their output is finished.

    `box_aligned` says the image already occupies its region rect exactly — true of every
    extraction, because the cut *is* the rect. Trimming such an image is actively harmful:
    `trim_and_pad` adds an 8% transparent margin (right for a generation, which arrives as
    a subject floating in an arbitrary frame), and drawing that back into the rect renders
    the art ~18% undersized and off-centre. Measured on the ClashUp lobby, skipping the
    trim moved the child icons from the low 40s to the high 90s."""
    emit = emit or NoEmit()
    if not box_aligned:
        out = trim_for_fit(out, asset.nine_slice is not None, asset.aspect_ratio)
    out = fit_to_resolution(out, asset.resolution)
    processed = new_asset_path(db, asset, asset.name)
    out.save(processed)

    version = AssetVersion(
        asset_id=asset.id, provider=provider, model=model, composed_prompt=prompt,
        raw_path=raw_rel, processed_path=rel_path(processed),
        # What this version was actually made from. Recorded even for extractions, where
        # the "reference" is the screenshot crop the pixels were cut out of — without it
        # nothing screen-derived can show where it came from.
        reference_paths=reference_paths or [],
    )
    db.add(version)
    db.commit()
    asset.selected_version_id = version.id
    if asset.nine_slice is not None:
        # May come back None: the seeded default assumes every ui_element is a resizable
        # frame, but plenty aren't (a nav bar full of icons has no stretchable band), and
        # slicing a non-frame smears its content when scaled.
        with Image.open(abs_path(version.processed_path)) as img:
            asset.nine_slice = detect_borders_if_frame(img)
        if asset.nine_slice is not None and region is not None:
            asset.nine_slice = _slice_if_it_helps(db, asset, version, region)
    db.commit()

    if region is not None:
        try:
            from ..scoring import rescore_region
            rescore_region(db, get_mockup_or_404(db, region.mockup_id), region)
        except Exception:
            pass  # scoring is diagnostic; never fail a good asset over it
    return version


def _slice_if_it_helps(db: Session, asset: Asset, version, region: MockupRegion):
    """Keep the detected borders only if rendering with them beats rendering without.

    `detect_borders_if_frame` decides from pixels alone whether a sprite is a stretchable
    frame, and it is asked to do something genuinely hard: tell a frame's border from a
    seam, a gloss line, or the gap between an icon and the first letter of a caption. Every
    guard it grew — a per-side cap, a symmetry ratio, a minimum stretchable band — rejects
    one more way of being wrong, and each one that gets added leaves a narrower way of
    being wrong behind it. The currency pill and the GAME MODE button squeezed through all
    three at once and cost 40 points apiece.

    There is no need to keep guessing, because the question has a measurable answer. The
    sprite is composited into a known rect and compared against a known reference crop, so
    just render it both ways and keep the better one. That is the objective the heuristics
    are approximations of, and it is available here for the price of two composites.

    Applies only to extractions, where a reference crop exists to compare against. A
    generated asset has nothing to measure against and keeps the detector's verdict.
    """
    from ..scoring import score_version_against_region

    borders = asset.nine_slice
    try:
        mockup = get_mockup_or_404(db, region.mockup_id)
        asset.nine_slice = borders
        sliced = score_version_against_region(version, asset, mockup, region)
        asset.nine_slice = None
        plain = score_version_against_region(version, asset, mockup, region)
    except Exception:
        return borders  # scoring is diagnostic; never drop good borders over it
    finally:
        asset.nine_slice = borders

    if sliced is None or plain is None:
        return borders
    return borders if sliced["score"] >= plain["score"] else None


def _extract_asset_for_region(
    db: Session, region: MockupRegion, atlas_id: int | None = None,
    resolution: str | None = None, emit=None, background: bool = False,
    method: str = "auto",
) -> Asset:
    """Cut the region's element straight out of the screenshot — no provider, no quota.

    This is the default path. The screenshot already holds the element's exact pixels, so
    segmenting them beats asking a model to redraw them: measured on the ClashUp lobby,
    extraction scores ~85/100 against the reference where regeneration scores ~36 (see
    processing/fidelity.py and tools/score_run.py).

    `background=True` means other elements sit on top of this one, so its interior is
    inpainted clean to yield a reusable empty frame.

    Text is never touched here regardless of any caption's `text_mode` — those are
    handled afterward, by the Text step's own AI pass (`_apply_text_choices`), which
    is the only place lettering gets removed or lifted off. See its docstring for why."""
    emit = emit or NoEmit()
    mockup = get_mockup_or_404(db, region.mockup_id)
    project = get_project_or_404(db, mockup.project_id)
    src = abs_path(mockup.image_path)
    if not src.exists():
        raise HTTPException(400, "Mockup image file missing")

    target_res = resolution or region.resolution or _region_target_resolution(mockup, region)
    asset = _ensure_region_asset(db, region, project, target_res, atlas_id, resolution, "extract")

    emit.emit("extract", "running", f"Extracting {region.name} from the screen…")
    with Image.open(src) as shot:
        shot = shot.convert("RGB")
        # De-occlusion happens HERE, on the screenshot, before anything is cut out of it.
        # Erasing the sword from the picture and then segmenting the button gives the
        # button's real outline and its real colour underneath; segmenting first and
        # patching the sprite afterwards can only ever guess at both. See
        # processing/inpaint.py for the defects that ordering produced.
        clean, cleared = _deoccluded_screenshot(
            db, shot, mockup, region, emit=emit, background=background,
        )
        # One super-resolution pass per screenshot, cached on disk and shared by every
        # region on it. Extraction then runs at 4x, which is what makes the edges crisp:
        # a nav icon is 94px wide in the source and its outline is a single pixel, so
        # there is nothing for a matte to resolve until there are more pixels to resolve it
        # in. A de-occluded screenshot is a different image, so it gets its own cache slot.
        source = _supersampled(clean, src if cleared == "none" else None)
        # Segment on the model's enlargement, but paint from a plain interpolation of the
        # real screenshot — Real-ESRGAN invents the detail it adds, and a x4 round trip
        # through it shifts colour by ΔE 2.57 against 0.38 for cubic.
        paint = _plain_upsample(clean, source[1])
        # Always segment from the rect the detector proposed, not from whatever a previous
        # build grew it to — growth compounds otherwise, and a box creeps outward on every
        # rebuild until it swallows its neighbours.
        base_rect = tuple(region.detect_rect or (region.x, region.y, region.w, region.h))
        if region.detect_rect is None:
            region.detect_rect = list(base_rect)
        cut, backend, grown = extract_region(
            clean, base_rect, method=method, source=source, colour=paint,
            # Standalone buttons have clear room around them and are exactly what carries a
            # cast shadow or an outer keyline SAM2's own mask doesn't claim — see
            # extract_region's shadow_margin doc. Left off for icons/sprites/tiles (often
            # packed tight against a neighbour, where the same margin would just as happily
            # start recovering the neighbour's edge instead of a shadow) AND for `background`
            # frames: those are already flush against whatever sits in/on them, already
            # de-occluded, and isolated fidelity scoring already special-cases them (see
            # rescore_region) — growing their box on top of that stacks badly.
            shadow_margin=(region.asset_type == "ui_element" and not background),
        )
        # The segmenter is allowed to correct a box that was clipping its element. Write
        # the rect back before anything downstream reads it: the child-box maths, the
        # compositor and the Unity layout all place this sprite by the region rect, so a
        # sprite cut from a wider box than the region records would land misaligned.
        if grown != (region.x, region.y, region.w, region.h):
            # The rect can come back SMALLER than it went in — the bleed retry ladder is
            # allowed to tighten a box whose mask ate a neighbour. Saying "widened" either
            # way is how a 10% crop reads to the user as their own resize being ignored,
            # so report which direction it actually moved.
            tightened = grown[2] < region.w or grown[3] < region.h
            note = (
                "tightened — the mask was picking up its surroundings" if tightened
                else "widened to fit the element it was clipping"
            )
            emit.emit(
                "extract", "running", f"{region.name}'s box was {note}",
                data={"was": [region.x, region.y, region.w, region.h], "now": list(grown)},
            )
            region.x, region.y, region.w, region.h = grown
            db.commit()
        if cleared != "none":
            backend = f"{backend}+{cleared}"

    raw = new_asset_path(db, asset, f"{asset.name}-raw")
    cut.save(raw)
    emit.emit(
        "extract", "done", f"Extracted {region.name} ({backend})",
        image=emit.preview(cut, "extract"), data={"backend": backend, "source": "extract"},
    )

    # The crop this was cut out of is this asset's reference, and the only provenance an
    # extraction has. Cheap to write and it is what the Assets tab shows under the prompt.
    try:
        crop_rel = [_crop_region(db, region, asset)]
    except HTTPException:
        crop_rel = []

    _finalize_version(
        db, asset, cut, provider=f"extract:{backend}", model=None,
        prompt=region.prompt or "", raw_rel=rel_path(raw), region=region, emit=emit,
        box_aligned=True, reference_paths=crop_rel,
    )

    db.refresh(asset)
    return asset


def _text_asset_name(parent: MockupRegion, label: MockupLabel) -> str:
    """A name that says which element the lettering came off and which run it was.

    Named after the words themselves ("PlayButton Text PLAY") rather than numbered, because
    the number is positional: insert a label, re-run detection, and every text sprite after
    it silently rebinds to different artwork. The string is stable as long as the caption is.
    """
    words = re.sub(r"[^A-Za-z0-9]+", " ", label.text or label.name or "").strip()
    suffix = "".join(w[:1].upper() + w[1:] for w in words.split())[:40]
    return f"{parent.name} Text{' ' + suffix if suffix else ''}"


def _text_assets(
    db: Session, mockup: Mockup, project: Project, parent: MockupRegion,
    layers: list, atlas_id: int | None, emit=None,
) -> list[Asset]:
    """Turn each lifted text layer into a region + asset sitting on top of `parent`.

    A region, not a bare asset, because a region is what makes an asset part of the screen:
    the preview compositor, the screen export and the Unity layout all walk regions, and
    they draw largest-first, so a caption's box — necessarily smaller than the element it
    was printed on — lands on top of that element without needing a z-order of its own.

    Rebuilt in place. The same caption extracted twice is the same sprite, so it rebinds its
    existing region rather than stacking a second one on the first — otherwise every rebuild
    of a nav bar would leave another five text regions behind it.
    """
    emit = emit or NoEmit()
    W, H = _mockup_size(mockup)
    if not W or not H:
        return []
    made = []
    for layer in layers:
        label, sprite, (bx0, by0, bx1, by1) = layer["label"], layer["image"], layer["box"]
        name = _text_asset_name(parent, label)
        x, y = 100.0 * bx0 / W, 100.0 * by0 / H
        w, h = 100.0 * (bx1 - bx0) / W, 100.0 * (by1 - by0) / H

        region = next(
            (
                r for r in mockup.regions
                if r.source == TEXT_SOURCE and r.name == name and r.id != parent.id
            ),
            None,
        )
        if region is None:
            region = MockupRegion(
                mockup_id=mockup.id, name=name, source=TEXT_SOURCE,
                color=TEXT_REGION_COLOR, asset_type="icon",
                # What the sprite is a picture of. Reads as a prompt because everything
                # downstream (the Assets tab, a later regenerate) expects one there.
                prompt=f'The text "{label.text}" as it appears on {parent.name}',
            )
            db.add(region)
        region.x, region.y, region.w, region.h = x, y, w, h
        region.detect_rect = [x, y, w, h]
        region.resolution = calculate_region_resolution(W, H, w, h)
        region.force_rebuild = False
        db.commit()

        asset = _ensure_region_asset(
            db, region, project, region.resolution, atlas_id, region.resolution, "extract"
        )
        asset.type = "icon"
        # Same reasoning as the parent's own polish pass (see _upscaled_resolution): the
        # sprite just came from a `text_only`+`upscale` reference-mode redraw, so the
        # target box needs raising too or the extra fidelity never survives fit_to_resolution.
        asset.resolution = _upscaled_resolution(region.resolution)
        raw = new_asset_path(db, asset, f"{asset.name}-raw")
        sprite.save(raw)
        _finalize_version(
            db, asset, sprite, provider="llm:text", model=None,
            prompt=region.prompt or "", raw_rel=rel_path(raw), region=region, emit=emit,
            # box_aligned: the caller (_redraw_built_regions) already trimmed/padded this
            # sprite to its reference crop's own real aspect ratio before handing it here —
            # see the comment there for why that can't be `asset.aspect_ratio` itself.
            box_aligned=True,
        )
        emit.emit(
            "text", "done", f'Extracted "{label.text}" off {parent.name}',
            image=emit.preview(sprite, "text"), data={"region_id": region.id, "asset_id": asset.id},
        )
        made.append(asset)
    return made


def _drop_stale_text_children(
    db: Session, mockup: Mockup, parent: MockupRegion, kept: set[str], emit=None,
) -> None:
    """Remove text sprites this element used to have and no longer does.

    A text child only makes sense as the counterpart of a parent that had that lettering
    erased from it. The parent has just been re-cut, so anything it did not produce this
    time is now a duplicate of pixels that are back on the frame — turn extraction off, or
    reword the caption, and the old sprite would otherwise keep floating over the element
    showing text that is already printed underneath it.

    Safe to delete for the same reason a retired split asset is: it is derived data with a
    deterministic source, reproduced by re-running the parent. Ownership is by name prefix,
    which is exactly how `_text_asset_name` assigns it.
    """
    emit = emit or NoEmit()
    prefix = f"{parent.name} Text"
    stale = [
        r for r in mockup.regions
        if r.source == TEXT_SOURCE and r.name.startswith(prefix) and r.name not in kept
    ]
    if not stale:
        return
    asset_ids = [r.asset_id for r in stale if r.asset_id]
    for r in stale:
        emit.emit("text", "done", f"Dropped {r.name} (no longer extracted off {parent.name})")
        db.delete(r)
    db.commit()
    for asset_id in asset_ids:
        _retire_split_asset(db, asset_id)
    db.commit()


def _llm_reference_pass(
    db: Session, project: Project, ref_path: Path, ops: list[str], asset_name: str,
    provider_name: str, emit=None, verb: str = "Redrawing",
    model: str | None = None, params: dict | None = None, prompt_variant: str = "v1",
    dynamic_ops: dict[str, str] | None = None,
) -> Image.Image:
    """Redraw the image at `ref_path` via the image provider in reference mode, ticked to
    `ops` (see prompting.REFERENCE_OPS) — the shared call `_redraw_built_regions` uses for
    both whole-element cleanup and per-label text isolation. `verb` only changes the
    progress wording (e.g. "Polishing" vs "Removing text on" vs "Isolating") to match
    whichever step is actually calling this.

    `model`, `params` and `prompt_variant` exist so the model/params/wording used for these
    two steps can be chosen per call and measured against each other, rather than being
    fixed at whatever the provider default happens to be. All three fall back to today's
    behaviour when omitted (saved pref, no extra params, shipped wording), so an
    un-migrated caller is unaffected.

    `dynamic_ops` overrides one or more of `ops`' static instruction text for this call
    only — the Text step uses it to tell the model exactly which captions to erase and
    which to leave untouched, see `prompting.build_text_removal_instruction`.

    Raises ProviderError on failure. Callers propagate it (stopping the build for that
    region with a clear error) rather than silently falling back to the pre-redraw image —
    a build where some regions were silently downgraded and others weren't would be a
    worse outcome than a clear stop.
    """
    emit = emit or NoEmit()
    prompt = reference_instruction(ops, prompt_variant, dynamic=dynamic_ops) + CHROMA_HINT
    provider = get_enabled_provider(provider_name)
    # The Text step ticks `text_only`, so it is an extraction and gets the same preparation
    # the asset path gives one: the model pinned for the job and a reference letterboxed
    # onto magenta into a canvas that model can draw. Without the letterbox the shipped
    # extraction wording would be describing an image the model never received.
    extracting = bool(extraction_op_keys() & set(ops or []))
    model = (resolve_extraction_model(provider_name, model) if extracting
             else resolve_model(provider_name, model))
    params = resolve_params(provider_name, model, params)
    if extracting:
        warning = extraction_model_warning(provider_name, model)
        if warning:
            emit.emit("model", "done", f"⚠ {warning}", data={"details": warning})
        with Image.open(ref_path) as im:
            ratio = snap_ratio(im.width, im.height, model_aspect_options(provider_name, model))
        ref_path = letterbox_reference(
            ref_path, new_work_path(project.id, f"{asset_name}-extract-ref"), ratio,
        )
    raw = new_work_path(project.id, f"{asset_name}-redraw-raw")
    emit.emit(
        "polish", "running", f"{verb} {asset_name} · {provider.name}/{model or 'default'}…",
        data={"ops": ops},
    )
    provider.generate(
        prompt, raw, reference_images=[ref_path], transparent=True, model=model,
        reference_mode=True, params=params,
    )
    # Said out loud, because it is the whole point of the recovery path: this image came
    # from a job the account had already been charged for by an attempt that died, so the
    # re-run cost nothing. Silently reusing it would look identical to paying twice.
    recovered = getattr(provider, "last_recovery", None)
    if recovered:
        emit.emit(
            "polish", "done",
            f"{asset_name} — reused the image an earlier attempt already paid for "
            f"(job {recovered[:8]}, no new charge)",
            data={"recovered_job": recovered},
        )
    with Image.open(raw) as img:
        out = remove_background(img) if provider.needs_transparency_postprocess else img.convert("RGBA")
    emit.emit("polish", "done", f"{asset_name} redrawn", image=emit.preview(out, "polish"))
    return out


def _upscaled_resolution(resolution: str) -> str:
    """Double `resolution`, capped at MAX_RESOLUTION_DIM — same doubling the standalone
    `/assets/{id}/upscale` endpoint does.

    Computed from a freshly-derived CANONICAL resolution (`_region_target_resolution`'s
    or `calculate_region_resolution`'s output, never `asset.resolution` as it currently
    stands), so re-running polish on an already-polished asset targets the same 2x size
    again rather than doubling every time and compounding without bound.

    Polish asks the model to redraw "at markedly higher fidelity" (the `upscale` op), but
    that's wasted if the result is then immediately squeezed back into the SAME box a
    plain extraction targets: `fit_to_resolution` only ever shrinks to fit (upscaling a
    low-res photographic extraction would invent detail that was never really there), so
    a redraw whose content came out smaller than that box was silently kept at its
    smaller native size — measured on GameModeButton, the polished asset landed at
    exactly the same 275x192 the plain CV extraction already was, none of the extra
    fidelity the model was asked for actually reflected in the stored asset. Raising the
    target box is what lets that fidelity survive `fit_to_resolution` instead of being
    thrown away by it.
    """
    w, h = parse_resolution(resolution)
    return f"{min(w * 2, MAX_RESOLUTION_DIM)}x{min(h * 2, MAX_RESOLUTION_DIM)}"


def _last_build_version_id(asset: Asset) -> int:
    """Newest version id that came from a *build* — an extraction (`extract:<backend>`) or
    a from-scratch generation (the provider's own name) — rather than from one of the
    redraw passes, which record themselves as `llm:text` / `llm:polish`.

    The floor for "has this pass already run on the element as it stands today": a Polish
    output that predates the current build belongs to artwork that has since been replaced,
    so it doesn't count as done, while one made after it does — regardless of whether the
    other redraw pass ran in between and now holds the selected slot."""
    return max((v.id for v in asset.versions if not (v.provider or "").startswith("llm:")), default=0)


def _redraw_is_current(asset: Asset | None, version_provider: str) -> bool:
    """Whether `version_provider`'s pass has already produced a still-present image for
    this asset's current build — i.e. re-running it would pay the provider for work that
    is already sitting in the catalogue.

    The file has to still exist: a version row whose PNG was deleted is not a result, and
    "we already have it" must mean the pixels, not the bookkeeping."""
    if not asset:
        return False
    floor = _last_build_version_id(asset)
    return any(
        # `+`-suffixed too: a later step that derives a version from this one keeps the
        # original as a prefix (`llm:polish+upscale:esrgan`, see the upscale endpoint), and
        # upscaling a polished sprite plainly does not mean it needs polishing again.
        (v.provider == version_provider or (v.provider or "").startswith(version_provider + "+"))
        and v.id > floor
        and v.processed_path and abs_path(v.processed_path).exists()
        for v in asset.versions
    )


def _text_child_sprite(
    db: Session, mockup: Mockup, parent: MockupRegion, label: MockupLabel
) -> Asset | None:
    """The already-extracted sprite for this caption, if it exists and its image is still
    on disk. Bound by name (`_text_asset_name`), which is how extraction assigns ownership
    in the first place — see `_drop_stale_text_children`."""
    name = _text_asset_name(parent, label)
    region = next(
        (r for r in mockup.regions if r.source == TEXT_SOURCE and r.name == name and r.id != parent.id),
        None,
    )
    if region is None or not region.asset_id:
        return None
    asset = db.get(Asset, region.asset_id)
    version = asset.selected_version if asset else None
    if not version or not version.processed_path or not abs_path(version.processed_path).exists():
        return None
    return asset


def _redraw_built_regions(
    db: Session, project: Project, mockup: Mockup, regions: list[MockupRegion],
    atlas_id: int | None, provider_name: str,
    base_ops: list[str], apply_text_choice: bool, raise_resolution: bool,
    version_provider: str, running_label: str, done_label: str, emit=None,
    model: str | None = None, params: dict | None = None, prompt_variant: str = "v1",
    force: bool = False,
) -> dict:
    """Shared AI-redraw worker for a chosen set of already-built regions, using each
    region's just-built asset image as the reference — the CV extraction supplies the
    accurate, correctly-cropped starting point the LLM redraws from, which is why this
    two-step process beats asking the model to invent the element from scratch (see
    `_generate_asset_for_region`'s ~36/100 fidelity for from-scratch regeneration vs
    extraction's ~85-93/100).

    `base_ops` (e.g. upscale/clean_edges/keep_colors for the Polish step, or empty for a
    text-only pass) are applied to every region; when `apply_text_choice`, each of the
    region's OWN captions' Remove/Extract choices from the Text step are additionally
    folded into ONE combined `remove_text` instruction naming exactly which captions to
    erase and which to leave alone (see `build_text_removal_instruction`) — never a
    run-wide override, and never one call per caption: a region with a caption on Remove
    and another on Keep still gets a single smart base redraw, not two.

    `raise_resolution` doubles the asset's target box (see `_upscaled_resolution`) so an
    `upscale` op's extra fidelity survives `fit_to_resolution` — only meaningful when
    `base_ops` actually asks for it; a text-only pass leaves resolution alone since it
    isn't asking the model to invent any extra detail worth preserving.

    **Resumes by default.** Every one of these calls is billed, and both steps are normally
    fired at a whole screen at once, so the run that fails on element 6 of 9 has already
    bought five images. Re-running it used to buy them a second time (and, for Polish,
    redraw an already-redrawn sprite — a compounding quality loss, see the step's own
    warning). So each region is checked against what is already in the catalogue first —
    `_redraw_is_current` for the base redraw, `_text_child_sprite` per caption — and only
    the genuinely missing work is sent to the provider. `force=True` overrides that for the
    case where a redo is the actual intent (the per-element buttons, or the explicit
    "redo everything" toggle).

    Returns a summary of what it actually did: {"processed", "skipped", "sprites",
    "sprites_skipped"} — the counts the caller reports so a resumed run says how much it
    reused rather than silently doing less than it was asked.
    """
    emit = emit or NoEmit()
    W, H = _mockup_size(mockup)
    to_process = [r for r in regions if r.asset_id and r.source != TEXT_SOURCE]
    total = len(to_process)
    summary = {"processed": 0, "skipped": 0, "sprites": 0, "sprites_skipped": 0}

    for idx, region in enumerate(to_process, 1):
        if getattr(emit, "is_stopped", False):
            break
        asset = db.get(Asset, region.asset_id)
        version = asset.selected_version if asset else None
        if not asset or not version or not version.processed_path:
            continue
        img_path = abs_path(version.processed_path)
        if not img_path.exists():
            continue

        # Each caption on this region has its OWN Keep/Remove/Extract choice (see
        # MockupLabel.text_mode) — split them once so the base redraw below can be told
        # exactly which words to erase and which to leave alone, and so the extraction
        # loop further down only fires for captions actually marked Extract.
        region_labels = _label_items_within(mockup, region, W, H) if apply_text_choice and W and H else []
        erase_items = [(l, box) for l, box in region_labels if l.text_mode == "erase"]
        extract_items = [(l, box) for l, box in region_labels if l.text_mode == "extract"]
        keep_items = [(l, box) for l, box in region_labels if l.text_mode not in ("erase", "extract")]
        remove_text = apply_text_choice and bool(erase_items or extract_items)
        extract_text = apply_text_choice and bool(extract_items)

        # What of this region's work already exists. `base_done` means the redraw itself
        # landed (on the build the element currently stands on); `reused` are the caption
        # sprites already sitting in the catalogue, which must be counted as kept or the
        # stale-child sweep at the bottom would delete the very sprites we just decided
        # not to re-buy.
        base_done = not force and _redraw_is_current(asset, version_provider)
        reused: list[str] = []
        todo_items = list(extract_items)
        if base_done and extract_text:
            todo_items = []
            for label, box in extract_items:
                if _text_child_sprite(db, mockup, region, label):
                    reused.append(_text_asset_name(region, label))
                else:
                    todo_items.append((label, box))
        if base_done and not todo_items:
            summary["skipped"] += 1
            summary["sprites_skipped"] += len(reused)
            emit.emit(
                "region", "done",
                f"{region.name} — already {done_label.lower()}, kept as is (no provider call)",
                index=idx, total=total,
            )
            _drop_stale_text_children(db, mockup, region, set(reused), emit)
            continue

        # No `fix_symmetry` here: it tells the model every border/corner should match,
        # which only holds for a genuinely symmetric button/frame. Nothing here says
        # whether a given `ui_element` region actually looks like that or is a bar/panel
        # that's rounded or trimmed on one side by design (a bottom nav bar, rounded only
        # at the top) — measured on exactly that case, forcing symmetry is what turned a
        # plain bar into an invented bordered panel with a fabricated header stripe.
        ops = list(base_ops)
        dynamic_ops = None
        if remove_text:
            ops.append("remove_text")
            dynamic_ops = {"remove_text": build_text_removal_instruction(
                [l.text for l, _ in erase_items + extract_items],
                [l.text for l, _ in keep_items],
            )}

        emit.emit(
            "region", "running",
            f"{running_label} {region.name}…" if not base_done
            else f"{region.name} — already {done_label.lower()}, only the missing text sprites left…",
            index=idx, total=total,
        )
        if base_done:
            summary["skipped"] += 1
            summary["sprites_skipped"] += len(reused)
        else:
            try:
                polished = _llm_reference_pass(
                    db, project, img_path, ops, asset.name, provider_name, emit,
                    verb=running_label, model=model, params=params, prompt_variant=prompt_variant,
                    dynamic_ops=dynamic_ops,
                )
            except ProviderError as e:
                msg = str(e)
                emit.emit("polish", "error", msg, data={"quota_exceeded": True} if is_quota_error(msg) else None)
                raise HTTPException(502, msg)
            summary["processed"] += 1

            # The LLM's output is a freeform generation on its own arbitrary canvas (commonly
            # square, whatever the element's actual shape) rather than a pixel-exact cut of the
            # rect, so it has to be cropped to content and padded back to the right proportions
            # before fit_to_resolution scales it into the target resolution box — otherwise the
            # asset comes out shrunk onto an oversized square canvas instead of its real shape.
            # The ratio to pad to has to come from THIS reference image's own real pixel
            # dimensions, not `asset.aspect_ratio`: that field is derived from the region's
            # rect as raw x/y PERCENTAGES of the mockup (`ratio_string(region.w, region.h)`),
            # which only equals the true pixel aspect ratio on a square mockup — measured on
            # this (768x1376) screen, PlayButton's stored ratio was 4.72:1 while its actual cut
            # is 2.64:1.
            with Image.open(img_path) as ref_img:
                ref_ratio = ratio_string(*ref_img.size)
            polished = trim_for_fit(polished, asset.nine_slice is not None, aspect_ratio=ref_ratio)

            if raise_resolution:
                # Raise the target box so the extra fidelity the model was just asked for (the
                # `upscale` op) actually lands in the stored asset instead of being shrunk back
                # down to whatever a plain extraction already targeted — see _upscaled_resolution.
                asset.resolution = _upscaled_resolution(_region_target_resolution(mockup, region))

            raw = new_asset_path(db, asset, f"{asset.name}-{version_provider.rsplit(':', 1)[-1]}")
            polished.save(raw)
            _finalize_version(
                db, asset, polished, provider=version_provider, model=None,
                prompt=region.prompt or "", raw_rel=rel_path(raw), region=region, emit=emit,
                box_aligned=True, reference_paths=[rel_path(img_path)],
            )

        # Lettering, isolated and saved per label rather than batched for the whole
        # region — a region like NavBarFrame carries several independent captions ("Shop",
        # "Cards", ...), and one call would return them all fused into a single image with
        # no way to split them back into separate sprites. Persisting each label's sprite
        # as soon as it's produced (rather than collecting the full list and filing them
        # all at the end) means a slow or hung provider call on one caption doesn't cost
        # the ones that already finished — measured live on this exact region: a stalled
        # call on "Cards" would otherwise have silently discarded "Shop"'s completed sprite.
        made: list[Asset] = []
        if extract_text and W and H:
            region_box = (
                int(W * region.x / 100.0), int(H * region.y / 100.0),
                int(W * (region.x + region.w) / 100.0), int(H * (region.y + region.h) / 100.0),
            )
            gx0, gy0 = region_box[0], region_box[1]
            with Image.open(abs_path(mockup.image_path)) as full_shot:
                full_rgb = full_shot.convert("RGB")
                for label, box in todo_items:
                    screen_box = (gx0 + box[0], gy0 + box[1], gx0 + box[2], gy0 + box[3])
                    local_box = _pad_box(screen_box, frac=0.4, clip=region_box)
                    crop_path = new_work_path(project.id, f"{asset.name}-{label.name or label.text}-src")
                    full_rgb.crop(local_box).save(crop_path)
                    try:
                        sprite = _llm_reference_pass(
                            db, project, crop_path, ["text_only", "upscale"],
                            f"{asset.name} Text {label.text}", provider_name, emit, verb="Isolating",
                            model=model, params=params, prompt_variant=prompt_variant,
                        )
                        # Pad to the CAPTION's own box shape, not the padded reference
                        # crop's — see the matching comment above for why this can't be
                        # `asset.aspect_ratio` either. Text sprites are never 9-sliced.
                        label_ratio = ratio_string(screen_box[2] - screen_box[0], screen_box[3] - screen_box[1])
                        sprite = trim_for_fit(sprite, False, aspect_ratio=label_ratio)
                    except ProviderError as e:
                        msg = str(e)
                        emit.emit(
                            "polish", "error", f'"{label.text}": {msg}',
                            data={"quota_exceeded": True} if is_quota_error(msg) else None,
                        )
                        # No cleanup here: `made` is only the labels reached before this
                        # one failed, not the true current set (later labels in this same
                        # loop haven't run yet, but still exist from a prior successful
                        # pass) — cleanup only knows the real current set once every label
                        # has been attempted, at the success path below. Deleting on a
                        # partial run was verified live to wipe already-good sprites
                        # (NavBarFrame's Shop/Cards/Battle captions vanished when the very
                        # first caption hit a transient CLI auth failure).
                        raise HTTPException(502, msg)
                    made.extend(_text_assets(
                        db, mockup, project, region,
                        [{"label": label, "image": sprite, "box": screen_box}], atlas_id, emit,
                    ))
                    summary["sprites"] += 1

        # `reused` belongs in `kept` alongside what this run made: those sprites are still
        # the correct counterpart of the (unchanged) parent, and the sweep deletes by
        # absence — omitting them would drop the very captions the resume just avoided
        # paying for.
        _drop_stale_text_children(db, mockup, region, {a.name for a in made} | set(reused), emit)

        emit.emit("region", "done", f"{done_label} {region.name}", index=idx, total=total)

    return summary


# How much of a smaller element must land on a frame before the frame is treated as
# occluded by it. Low on purpose — see `_occluding_regions`.
OCCLUDE_MIN_OVERLAP = 0.15
# ...and how much of a frame may be painted out before rebuilding it stops being recovery
# and starts being invention. Past this the frame keeps its occluders: still the reference's
# own pixels, just not reusable, which is a downgrade rather than a defect.
OCCLUDE_MAX_COVERAGE = 0.72

# `MockupRegion.source` marking a region the pipeline produced rather than detected: a
# caption whose `text_mode` is "extract", lifted off its parent and given a box and an
# asset of its own.
TEXT_SOURCE = "text"
TEXT_REGION_COLOR = "#ff9f4a"


def _buildable_regions(mockup: Mockup) -> list[MockupRegion]:
    """The regions that stand for something on the screen, i.e. everything the detector
    found — but not the text sprites extraction itself produced.

    An extracted caption is an *output*: its pixels already came out of its parent, and the
    parent's own build erased them. Left in the general pool it would be treated as one more
    element on the screen and rebuilt like one — segmented out of the screenshot it is no
    longer in (its parent's rebuild painted it away), counted as an occluder its parent must
    erase a second time, and enough to make any frame it sits on "a background" even when
    the user never asked for text removal there.
    """
    return [r for r in mockup.regions if r.source != TEXT_SOURCE]


def _occluding_regions(parent: MockupRegion, others: list) -> list:
    """Everything drawn on top of `parent` — the selection rule, without any geometry.

    Overlap, not containment. Requiring an occluder to be *contained* in the frame it
    covers gets the common case wrong: a level badge hangs off the bottom corner of a
    profile banner, a gem overhangs its pill, an active-tab arch rises above the nav bar.
    Those are ~70-80% inside, they were skipped, and they stayed baked into the frame —
    which was exactly the complaint that "the profile banner has all three elements inside
    it". What matters is not how much of the icon is on the frame but that *any appreciable
    part of it is*, because that part is what has to be painted out.

    The rest of the rule comes from the renderer: elements are drawn largest-first, so
    anything smaller than `parent` lands on top of it. Same-size or larger neighbours are
    peers or its own container and are left alone.
    """
    parea = parent.w * parent.h
    found = []
    for other in others:
        if getattr(other, "id", None) == parent.id and type(other) is type(parent):
            continue
        if other.w * other.h >= parea:
            continue
        if containment_ratio(
            (other.x, other.y, other.w, other.h),
            (parent.x, parent.y, parent.w, parent.h),
        ) < OCCLUDE_MIN_OVERLAP:
            continue
        found.append(other)
    return found


def _region_sprite(db: Session, region: MockupRegion) -> Image.Image | None:
    """The RGBA sprite already extracted for a region, or None if it has none yet."""
    if region.asset_id is None:
        return None
    asset = db.get(Asset, region.asset_id)
    version = asset.selected_version if asset else None
    if version is None:
        return None
    path = abs_path(version.processed_path or version.raw_path)
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.convert("RGBA").copy()


def _occluder_items(
    parent: MockupRegion, others: list, w: int, h: int
) -> list[tuple[object, tuple[int, int, int, int]]]:
    """`(occluder, pixel box)` for everything drawn on top of `parent`, clipped to its crop.

    Selection is `_occluding_regions`; this adds the geometry. Boxes are clipped to the
    parent crop, so a badge that only clips a corner erases just that corner rather than
    dragging the mask outside the frame entirely.

    Still used for text runs, which have no sprite of their own to take a silhouette from.
    Icon occluders go through `_occluder_masks` instead, which uses each one's real alpha.

    The occluder is handed back alongside its box because a text run's own MockupLabel is
    what names the sprite `extract_text` cuts from it — zipping the two lists back together
    afterwards would misalign the moment one box is dropped for being degenerate.
    """
    px0, py0 = w * parent.x / 100.0, h * parent.y / 100.0
    pw, ph = w * parent.w / 100.0, h * parent.h / 100.0
    items = []
    for other in _occluding_regions(parent, others):
        bx0 = max(0.0, w * other.x / 100.0 - px0)
        by0 = max(0.0, h * other.y / 100.0 - py0)
        bx1 = min(pw, w * (other.x + other.w) / 100.0 - px0)
        by1 = min(ph, h * (other.y + other.h) / 100.0 - py0)
        if bx1 > bx0 and by1 > by0:
            items.append((other, (int(bx0), int(by0), int(bx1), int(by1))))
    return items


def _occluder_boxes(
    parent: MockupRegion, others: list, w: int, h: int
) -> list[tuple[int, int, int, int]]:
    """Just the geometry from `_occluder_items`."""
    return [box for _other, box in _occluder_items(parent, others, w, h)]


def _child_boxes_within(
    mockup: Mockup, parent: MockupRegion, w: int, h: int
) -> list[tuple[int, int, int, int]]:
    """The foreground pieces (icons, badges) sitting on `parent`, in its crop's pixels."""
    return _occluder_boxes(parent, _buildable_regions(mockup), w, h)


def _label_items_within(
    mockup: Mockup, parent: MockupRegion, w: int, h: int
) -> list[tuple[MockupLabel, tuple[int, int, int, int]]]:
    """`(label, pixel box)` for the text runs sitting on `parent`, relative to its crop."""
    return _occluder_items(parent, list(mockup.labels), w, h)


def _label_boxes_within(
    mockup: Mockup, parent: MockupRegion, w: int, h: int
) -> list[tuple[int, int, int, int]]:
    """Pixel boxes of the text runs sitting on `parent`, relative to its crop."""
    return [box for _label, box in _label_items_within(mockup, parent, w, h)]


def _pad_box(
    box: tuple[int, int, int, int], frac: float = 0.75, minimum: int = 10,
    clip: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """Grow `box` into a local neighbourhood for judging a fill against, sized to the box
    itself rather than to whatever frame it sits in.

    This is what `plausible_fill` compares a fill against when `region_box` is passed. A
    frame is not always one colour: an active tab's highlight, a currency pill's icon end,
    can differ sharply from the plain background beside it. Padding from the OCCLUDER's own
    box keeps the reference local to it — clipping to the frame (`clip`) still stops an
    edge-hugging occluder's reference from running off into whatever is outside the frame
    entirely (sky behind a pill's rounded end), but nothing here searches all the way across
    the frame to somewhere a different colour, which is what let a sword icon and its
    caption on a highlighted nav tab get judged against the plain blue bar beside it and
    both come back blue.
    """
    bx0, by0, bx1, by1 = box
    pw = max(minimum, int(round((bx1 - bx0) * frac)))
    ph = max(minimum, int(round((by1 - by0) * frac)))
    x0, y0, x1, y1 = bx0 - pw, by0 - ph, bx1 + pw, by1 + ph
    if clip is not None:
        cx0, cy0, cx1, cy1 = clip
        x0, y0, x1, y1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
    return (x0, y0, x1, y1)


def _occluder_masks(
    db: Session, mockup: Mockup, region: MockupRegion, w: int, h: int
) -> list[tuple["np.ndarray", tuple[int, int, int, int], bool]]:
    """`(mask, own_box, exact)` for each thing drawn on top of `region`, one entry per
    occluder rather than one mask for all of them, using each occluder's OWN extracted
    silhouette wherever one exists.

    Kept separate per occluder — see `_pad_box` — so a fill is judged against ITS OWN
    surroundings rather than the whole parent frame's, and a bad fill for one occluder can
    be rejected without discarding every other occluder cleared alongside it.

    The silhouette itself is still the fix for the rectangular scars: an occluder's bounding
    box is a poor stand-in for the occluder, erasing the nav icons' boxes off the bar cut
    five square notches out of the bar's top edge, because the boxes are taller than the bar
    and their corners are bar. The icons have already been extracted by the time the bar is
    built, though, and their alpha is their exact shape — a shield with a point, a sword on
    the diagonal — so erasing that leaves the bar's own geometry alone.

    Falls back to the box for an occluder that has not been extracted yet (a rebuild of one
    region on its own, or a generate-mode run), which is the old behaviour and still better
    than leaving the icon baked in.
    """
    occluders = _occluding_regions(region, _buildable_regions(mockup))
    out = []
    for other in occluders:
        box = (
            int(w * other.x / 100.0), int(h * other.y / 100.0),
            int(w * (other.x + other.w) / 100.0), int(h * (other.y + other.h) / 100.0),
        )
        sprite = _region_sprite(db, other)
        if sprite is not None:
            one = alpha_to_mask((w, h), sprite, box)
            exact = True
        else:
            one = boxes_to_mask((w, h), [box])
            exact = False
        # Grow past the silhouette to catch the antialiased rim and drop shadow the alpha
        # cut off; left behind they ring the hole with icon-coloured pixels.
        #
        # Scaled to the OCCLUDER, not the canvas. The skirt has to be proportional to the
        # thing being erased: one radius derived from the screenshot is ~15px on a phone
        # screenshot, which is a sane margin around a nav bar and a catastrophe around a
        # 50px gem — it took the currency capsule's entire left cap out with the gem and
        # the frame came back with its end missing. Per-occluder, so a big element still
        # gets a big skirt without a small one being erased along with its surroundings.
        ow, oh = box[2] - box[0], box[3] - box[1]
        one = dilate_mask(one, min(12, max(2, int(round(0.06 * min(ow, oh))))))
        if one.any():
            out.append((one, box, exact))
    return out


def _deoccluded_screenshot(
    db: Session, shot: Image.Image, mockup: Mockup, region: MockupRegion, emit=None,
    background: bool = False,
) -> tuple[Image.Image, str]:
    """The screenshot with icons/sprites that sit on `region` painted out of it.

    Returns (image, method) — the original and "none" when there is nothing to remove or
    inpainting is unavailable, in which case the element keeps what sat on it. That is
    still a truthful extraction, just not reusable, so it is a downgrade rather than a
    failure.

    Text removal/extraction is handled separately by the Text step's AI pass (see
    `_llm_reference_pass` / `_apply_text_choices`) rather than here — see git history for
    the CV inpaint/diff-matte approach this replaced.
    """
    import numpy as np

    emit = emit or NoEmit()
    w, h = shot.width, shot.height
    icon_items = _occluder_masks(db, mockup, region, w, h) if background else []
    if not icon_items:
        return shot, "none"

    region_box = (
        int(w * region.x / 100.0), int(h * region.y / 100.0),
        int(w * (region.x + region.w) / 100.0), int(h * (region.y + region.h) / 100.0),
    )

    # "Too much masked to infer from" has to be judged against the REGION, not the canvas.
    # The check lives here rather than in `deocclude` because that now works on the whole
    # screenshot, where any one element's occluders are a rounding error of the total area
    # and the guard would never fire — an icon covering three quarters of the pill it sits
    # on is 0.5% of the screen. What matters is how much of the thing being rebuilt is
    # gone, and past that point the sprite would be mostly invented. Judged on the UNION of
    # every occluder's mask — the decision to give up is region-wide even though the fills
    # themselves now happen one occluder at a time.
    bx0, by0, bx1, by1 = region_box
    union = np.zeros((h, w), bool)
    for m, _box, _exact in icon_items:
        union |= m > 0
    inside = union[max(0, by0):by1, max(0, bx0):bx1]
    if inside.size and float(inside.mean()) > OCCLUDE_MAX_COVERAGE:
        emit.emit(
            "inpaint", "done",
            f"{region.name} is {inside.mean() * 100:.0f}% covered — kept occluded "
            "rather than inventing it",
        )
        return shot, "none"

    exact_n = sum(1 for _, _, e in icon_items if e)
    what = f"{exact_n} icon silhouette(s)" if exact_n else "icon boxes"
    emit.emit("inpaint", "running", f"Clearing {what} off {region.name}…")

    note = lambda m: emit.emit("inpaint", "running", m)  # noqa: E731
    rgb = np.asarray(shot.convert("RGB"))

    # One occluder at a time, not one combined mask for everything on the region. A
    # plausibility check against the WHOLE parent frame (or a combined mask's own
    # statistics) blurs together parts of the frame that legitimately differ: an active
    # tab's highlight versus the plain bar beside it. Judged that way, a sword icon sitting
    # on a highlighted nav tab was compared against the bar's own (mostly plain blue)
    # pixels on average, and a fill that turned the tab blue passed — right on average,
    # wrong exactly where it mattered. `_pad_box` gives each occluder its own local
    # reference instead, so a bad fill for one item is caught and rejected without
    # discarding everything cleared alongside it.
    cleared, kept = 0, 0
    for mask_i, box_i, _exact in icon_items:
        local_box = _pad_box(box_i, clip=region_box)
        candidate, m = deocclude(rgb, mask_i, progress=note, region_box=local_box)
        if m != "none" and plausible_fill(candidate, mask_i, local_box):
            rgb = candidate
            cleared += 1
        else:
            kept += 1
    if kept:
        note(f"{region.name}: kept {kept} of {len(icon_items)} occluder(s) whose fill didn't match its surroundings")
    used = "lama" if cleared else "none"
    if used == "none":
        emit.emit("inpaint", "done", f"{region.name} kept its foreground (inpainting skipped)")
        return shot, "none"
    cleaned = Image.fromarray(rgb, "RGB")
    emit.emit(
        "inpaint", "done", f"Cleared {region.name} with {used}",
        image=emit.preview(cleaned, "inpaint"),
        data={"method": used, "exact_silhouettes": exact_n},
    )
    return cleaned, used


def _supersampled(img: Image.Image, cache_key_path=None):
    """`(rgb_array, scale)` for `img`, memoised on disk when it is the unmodified mockup.

    A de-occluded screenshot is a one-off — it differs per parent frame — so it is
    supersampled in memory. The original is shared by every region on it and goes through
    the on-disk cache, which is what keeps a fifteen-region build to a single model pass.
    """
    from ..processing import upres

    if cache_key_path is not None:
        return upres.supersample_cached(cache_key_path, img)
    return upres.supersample(img)


def _plain_upsample(img: Image.Image, scale: int):
    """The screenshot enlarged by simple interpolation — the sprite's colour source.

    Cubic invents nothing: it is a smooth reconstruction of pixels that are really there,
    so a sprite cut from it and resized back down to its export target carries the
    reference's own colours. That is the entire premise of extracting instead of
    generating, and it would be given away at the last step by taking pixels from a
    generative upscaler. `extract_region` documents the measurement behind this.
    """
    import cv2
    import numpy as np

    rgb = np.asarray(img.convert("RGB"))
    if scale <= 1:
        return rgb
    return cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def _region_target_resolution(mockup: Mockup, region: MockupRegion) -> str:
    src = abs_path(mockup.image_path)
    if src.exists():
        with Image.open(src) as img:
            return calculate_region_resolution(img.width, img.height, region.w, region.h)
    return DEFAULT_RESOLUTION


def _generate_asset_for_region(
    db: Session, region: MockupRegion, provider_name: str, atlas_id: int | None = None,
    resolution: str | None = None, emit=None, background: bool = False,
) -> Asset:
    """Create (or reuse) an Asset for the region and generate its first version using
    the mockup crop as a style/content reference. If atlas_id is given, a newly created
    asset is filed into that domain. `emit` (an Emitter) reports intermediate images.
    When `background` is set, the region is a container/frame that other elements sit on,
    so it is generated as an EMPTY frame (no inner icon/glyph/text) — its foreground pieces
    are separate sprites layered on top at composite time.

    Prefer `_extract_asset_for_region`: this redraws the element and drifts from the
    reference. It is the fallback for elements extraction can't recover."""
    emit = emit or NoEmit()
    mockup = get_mockup_or_404(db, region.mockup_id)
    project = get_project_or_404(db, mockup.project_id)

    target_res = resolution or region.resolution or _region_target_resolution(mockup, region)
    asset = _ensure_region_asset(db, region, project, target_res, atlas_id, resolution, "generate")

    crop_rel = _crop_region(db, region)
    crop_abs = abs_path(crop_rel)
    with Image.open(crop_abs) as cimg:
        emit.emit("crop", "done", f"Cropped {region.name} from screen", image=emit.preview(cimg, "crop"))

    style_refs, sibling = _style_references(db, project, asset, atlas_id)
    prompt = compose_prompt(project, asset)
    # Reference #1 is always the crop (the content to recreate). A same-family sibling,
    # when one exists, is reference #2 and pins the shared frame so siblings stay identical
    # bar their icon. Project style anchors trail as lowest-priority look references.
    references = [crop_abs]
    if sibling is not None:
        references.append(sibling)
        prompt += (
            "\nTake the FIRST reference image (the cropped element) and extract the element from it "
            "exactly as depicted in the reference image, preserving its exact shapes, artwork, details, and colors, "
            "isolated without its original background or surrounding mockup graphics. "
            "All edges must be sharp, clean, and crisp. "
            "If this is a frame/panel, the borders MUST be perfectly symmetric (left = right, top = bottom, all corners identical). "
            "The SECOND reference image is an existing element from the same UI family — match "
            "its frame shape, border thickness, corner radius, materials, shading and colors "
            "EXACTLY; only the inner icon, label or content should differ to match the first image."
        )
    else:
        prompt += (
            "\nTake the reference image (the cropped element) and extract the element from it "
            "exactly as depicted in the reference image, preserving its exact shapes, artwork, details, colors, and design, "
            "isolated without its original background or surrounding mockup graphics. "
            "All edges must be sharp, clean, and crisp. "
            "If this is a frame/panel, the borders MUST be perfectly symmetric (left = right, top = bottom, all corners identical)."
        )
    if background:
        prompt += (
            "\nExtract ONLY the EMPTY background frame/panel from the reference image: its shape, border, "
            "material and colors exactly as in the reference image, but isolated without background and with a COMPLETELY "
            "BLANK interior — no icon, glyph, symbol, arrow, number or text inside it, "
            "nothing but the empty frame. Borders MUST be perfectly symmetric on all four sides. "
            "All edges must be sharp, clean, and crisp so foreground art can be layered on top later."
        )
    references.extend(style_refs)
    ref_paths = [rel_path(r) for r in references if Path(r).exists()]

    try:
        provider = get_enabled_provider(provider_name)
    except ProviderError as e:
        raise HTTPException(403, str(e))
    raw = new_asset_path(db, asset, f"{asset.name}-raw")
    model = resolve_model(provider_name)
    toks = estimate_tokens(prompt, output="", image_count=len(references))
    gen_data = {
        "prompt": prompt,
        "provider": provider.name,
        "model": model,
        "reference_images": ref_paths,
        "tokens": toks,
    }
    emit.emit("generate", "running", f"Generating {asset.name} · {provider.name}/{model or 'default'}…", data=gen_data)
    try:
        provider.generate(prompt, raw, reference_images=references, transparent=True, model=model)
    except ProviderError as e:
        msg = str(e)
        emit.emit("generate", "error", msg, data={"quota_exceeded": True} if is_quota_error(msg) else gen_data)
        if asset and asset.selected_version_id is None:
            region.asset_id = None
            _delete_asset_files(db, asset)
            db.delete(asset)
            db.commit()
        raise HTTPException(502, msg)

    cmd_str = getattr(provider, "last_command", None)
    if cmd_str:
        gen_data["command"] = cmd_str
    emit.emit("generate", "done", f"Raw image generated for {asset.name}", data=gen_data)

    with Image.open(raw) as img:
        out = remove_background(img) if provider.needs_transparency_postprocess else img.convert("RGBA")
    done_data = {
        **gen_data,
        "tokens": estimate_tokens(prompt, output=f"Generated {asset.name} PNG", image_count=len(references)),
    }
    emit.emit("asset", "done", f"{asset.name} → {asset.resolution}", image=emit.preview(out, "final"), data=done_data)
    _finalize_version(
        db, asset, out, provider=provider.name, model=model, prompt=prompt,
        raw_rel=rel_path(raw), region=region, emit=emit, reference_paths=ref_paths,
    )
    db.refresh(asset)
    return asset


@router.post("/regions/{region_id}/extract-asset", response_model=AssetOut)
def extract_asset_from_region(
    region_id: int, body: RegionExtractBody, db: Session = Depends(get_db)
):
    """Cut this region's element straight out of the screenshot. Costs no provider quota
    and reproduces the reference pixels exactly — the default way to make an asset."""
    region = get_region_or_404(db, region_id)
    mockup = get_mockup_or_404(db, region.mockup_id)
    emit = make_emitter(mockup.project_id, entity_type="mockup", entity_id=mockup.id)
    bg_ids, _ = _resolve_containment(list(mockup.regions))
    try:
        result = _extract_asset_for_region(
            db, region, atlas_id=body.atlas_id, resolution=body.resolution,
            emit=emit, background=(region.id in bg_ids), method=body.method,
        )
        emit.emit("__done__", "done", "", data={"asset_id": result.id})
        return result
    except HTTPException as e:
        emit.emit("__error__", "error", str(e.detail))
        raise


@router.post("/regions/{region_id}/generate-asset", response_model=AssetOut)
def generate_asset_from_region(
    region_id: int, body: RegionGenerateBody, db: Session = Depends(get_db)
):
    region = get_region_or_404(db, region_id)
    mockup = get_mockup_or_404(db, region.mockup_id)
    emit = make_emitter(mockup.project_id, entity_type="mockup", entity_id=mockup.id)
    try:
        result = _generate_asset_for_region(
            db, region, body.provider, atlas_id=body.atlas_id, resolution=body.resolution, emit=emit,
        )
        emit.emit("__done__", "done", "", data={"asset_id": result.id})
        return result
    except HTTPException as e:
        emit.emit("__error__", "error", str(e.detail), data={"quota_exceeded": True} if is_quota_error(str(e.detail)) else None)
        raise


# --- Screen → Atlas: detect elements on an uploaded screen, then generate/reuse into a domain ---

# Only the full-screen scene/hero art is excluded — NOT UI background panels. A bare
# "background"/"bg" match used to wrongly drop wanted chrome like a NavBarBackground or a
# NamePlateBg; the scene backdrop is filtered instead by name (scene/environment/hero) and
# by the >60%-of-screen size guard in the detection loop.
EXCLUDE_NAME_PATTERN = re.compile(
    r"backdrop|environment|\bscene\b|\bhero\b|\bcharacter\b|\bskybox\b|\bwallpaper\b|"
    r"scene.?background|screen.?background|main.?background",
    re.I,
)


class DetectRegionsBody(BaseModel):
    llm: LlmSelection | None = None


def _parse_json_array(text: str) -> list[dict]:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise LlmError("Vision model did not return a JSON array")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LlmError(f"Could not parse detected regions: {e}")
    if not isinstance(data, list):
        raise LlmError("Detected regions payload was not a list")
    return data


# Vision models tokenize by image area (tiles); a screenshot far larger than this gains
# no detection accuracy but costs multiples of the tokens. Detection outputs are stored
# as percentages of the image, so operating on a downscaled copy is loss-free here.
MAX_VISION_DIM = 1568


def _downscaled_for_vision(img_path, run_dir=None):
    """Return (path, width, height, cleanup) for a vision-sized copy of img_path. If the
    image is already within MAX_VISION_DIM, the original is used untouched."""
    with Image.open(img_path) as im:
        w, h = im.size
        if max(w, h) <= MAX_VISION_DIM:
            return img_path, w, h, (lambda: None)
        scale = MAX_VISION_DIM / max(w, h)
        small = im.convert("RGB").resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    if run_dir is not None:
        dest = run_dir / "vision-input.png"
        small.save(dest)
        return dest, small.width, small.height, (lambda: None)
    import tempfile
    fd, name = tempfile.mkstemp(suffix=".png", prefix="vision-")
    from pathlib import Path as _P
    import os as _os
    _os.close(fd)
    dest = _P(name)
    small.save(dest)
    return dest, small.width, small.height, (lambda: dest.unlink(missing_ok=True))


def _detect_regions(db: Session, mockup: Mockup, body: "DetectRegionsBody", emit) -> Mockup:
    img_path = abs_path(mockup.image_path)
    if not img_path.exists():
        raise HTTPException(400, "Mockup image file missing")

    with Image.open(img_path) as orig:
        orig_w, orig_h = orig.size
    # token optimization: analyze a vision-sized copy (percentages are scale-invariant)
    vis_path, img_w, img_h, cleanup = _downscaled_for_vision(img_path)
    if (img_w, img_h) != (orig_w, orig_h):
        emit.emit("prepare", "done",
                  f"Downscaled screen {orig_w}×{orig_h} → {img_w}×{img_h} for analysis (fewer tokens)")

    provider, options = resolve_options(db, body.llm)
    runner = get_runner(provider)
    options.images = [vis_path]

    instruction = (
        f"Look at the image file at path: {vis_path}\n"
        "It is one screen of a mobile game UI. List every reusable UI element that should "
        "become its own isolated 2D sprite: icons, buttons, panels, badges, banners, "
        "currency/resource displays, nav bar items, decorative UI frames, etc. Do NOT "
        "include the full-screen background scene/environment art or the hero/character "
        "render — only self-contained UI chrome.\n"
        "\nCrucial Step 1 — BOUNDING BOXES. For each element output `box_2d` as "
        "[ymin, xmin, ymax, xmax] normalized to 0-1000 (0 = top/left edge of the image, "
        "1000 = bottom/right edge). Each box must tightly enclose the element's FULL visible "
        "extent — its entire rounded-rectangle/panel background and border — tracing the "
        "actual painted edges, with no empty margin and without cutting off the frame.\n"
        "A box is that ONE element's own painted shape and nothing else. Never stretch a "
        "box outward to swallow a different element that happens to overlap it: a name "
        "banner whose left end is hidden behind a round avatar is boxed as the BANNER "
        "(start the box where the banner's own bar begins, even if the avatar covers that "
        "end), and the avatar gets its own box. Overlapping boxes are expected and fine.\n"
        "\nCrucial Step 2 — SEPARATE EVERY FOREGROUND PIECE FROM ITS CONTAINER. A button, "
        "pill, panel or bar is a BACKGROUND FRAME; every icon or glyph drawn on top of it is "
        "a SEPARATE item. Emit the frame AND each icon as their own items, ALWAYS, however "
        "many icons the container holds. The frame is delivered empty and the icons are "
        "layered back on top, so an icon left inside its frame is permanently baked in and "
        "cannot be moved, restyled or reused.\n"
        "Examples:\n"
        "  • the blue frame of a PLAY button is one item; the sword glyph on it is a second item.\n"
        "  • the gold frame of a GAME MODE button is one item; the arrow glyph on it is a second item.\n"
        "  • a bottom nav bar with 5 nav icons → SIX items: the bar itself, plus each of the "
        "five icons (shop, cards, battle, social, events) as its own item. Give each nav icon "
        "its own tight box around just that glyph.\n"
        "  • a player profile cluster → THREE items, never one: the name banner/plate bar on "
        "its own, the round avatar portrait on its own (just the portrait and its ring, no "
        "banner and no badge), and the level badge on its own.\n"
        "\nCrucial Step 3 — TEXT IS ITS OWN TYPE, NEVER PART OF A SPRITE. Emit every run of "
        "text, numbers, letters or word-labels as an item with \"type\": \"text\" — button "
        "captions ('PLAY', 'GAME MODE'), currency amounts ('1,250'), the player name, level "
        "numbers, nav labels ('Shop', 'Cards'). Put the literal characters in \"text\" and the "
        "text's main colour as a hex string in \"color\". These do NOT become sprites: the game "
        "renders them with a real font, and their boxes are used to erase the lettering from "
        "the frames underneath so the frames come out blank.\n"
        "Do NOT let a text box drive the size of the element behind it — the PLAY button's box "
        "is the whole blue frame, and 'PLAY' is a separate text item on top of it.\n"
        "\nCrucial Step 4 — is_sliced: true for resizable frames (buttons, panels, bars), "
        "false for icons/coins/badges/avatars/glyphs. Sliced elements MUST have perfectly symmetric "
        "borders (left = right, top = bottom, all corners identical) for 9-slice scaling.\n"
        "\nCrucial Step 5 — SHARED TEMPLATES: if two or more elements are the SAME repeated "
        "component whose frame art is identical and they differ ONLY by their inner icon (e.g. a "
        "gem currency pill and a gold currency pill sharing one capsule), give each of them the "
        "SAME 'template' string (e.g. 'CurrencyPill') and put a short description of THAT one's "
        "distinguishing icon in 'icon' (e.g. 'faceted purple gem', 'gold coin') — and for these "
        "do NOT also emit a separate foreground box for that icon (the template handles it). "
        "Elements meant to look visually DISTINCT (a BLUE play button vs a GOLD game-mode button) "
        "must NOT share a template — set 'template' and 'icon' to null.\n"
        "\nCrucial Step 6 — target output resolution string ('resolution'). "
        f"Size it for a {REFERENCE_DEVICE_WIDTH}px-wide device: take the element's width as a "
        f"fraction of the screen, multiply by {REFERENCE_DEVICE_WIDTH}, and match the element's "
        "real aspect ratio (do NOT default everything to square). Never go below "
        f"{MIN_RESOLUTION_DIM}px on the long edge. Examples: a full-width nav bar -> '1440x160', "
        "a wide play button -> '768x192', a currency pill -> '512x128'.\n"
        "\nCrucial Step 7 — `prompt` is a SHORT description of the element's appearance, one "
        "sentence, no instructions and no boilerplate ('glossy blue rounded button frame with "
        "a thick gold bevelled border'). Elements are cut straight out of this screenshot, so "
        "the description is only a label and a fallback; length adds nothing.\n"
        "\nReply with ONLY a valid JSON array, no prose, no markdown fences. Item format:\n"
        '{"name": "PlayButton", "type": "ui_element", "is_sliced": true, '
        '"box_2d": [740, 110, 830, 590], "prompt": "glossy blue rounded button frame with a '
        'thick gold bevelled border", "resolution": "768x192", "template": null, "icon": null}\n'
        'Text item format:\n'
        '{"name": "PlayLabel", "type": "text", "box_2d": [757, 300, 812, 520], '
        '"text": "PLAY", "color": "#FFE9A8"}\n'
        "type must be one of: ui_element, icon, sprite, tile, sprite_sheet, text. "
        "name must be a short PascalCase identifier."
    )

    detect_data = {
        "prompt": instruction,
        "provider": provider,
        "model": options.model or "default",
        "tokens": estimate_tokens(instruction, image_count=1),
        "mockup_id": mockup.id,
    }

    with Image.open(vis_path) as vimg:
        emit.emit("detect", "running", "Detecting UI elements with vision model…",
                  image=emit.preview(vimg, "screen"), data=detect_data)

    img_path = vis_path  # everything below (instruction, refine) uses the vision copy

    try:
        raw = runner.run(instruction, options)
        items = _parse_json_array(raw)
    except LlmError as e:
        cleanup()
        raise HTTPException(502, str(e))

    cmd_str = getattr(runner, "last_command", None)
    if cmd_str:
        detect_data["command"] = cmd_str

    detect_done_data = {
        **detect_data,
        "raw_output": raw,
        "tokens": estimate_tokens(instruction, output=raw, image_count=1),
    }
    emit.emit("detect", "done", f"Vision model found {len(items)} candidate elements", data=detect_done_data)

    # Perform OpenCV contour edge snapping and Non-Maximum Suppression (NMS)
    emit.emit("refine", "running", "Snapping boxes to element edges…", data={"mockup_id": mockup.id})
    refined_items = filter_and_refine_regions(img_path, items, img_w, img_h)
    cleanup()
    emit.emit("refine", "done", f"Snapped {len(refined_items)} boxes to element edges", data={"mockup_id": mockup.id})

    # Replace unbound regions (not yet tied to a generated asset); keep bound ones.
    for r in list(mockup.regions):
        if r.asset_id is None:
            db.delete(r)
    db.commit()

    for label in list(mockup.labels):
        db.delete(label)

    for item in refined_items:
        name = str(item.get("name") or "").strip()
        if not name or EXCLUDE_NAME_PATTERN.search(name):
            continue
        raw_type = str(item.get("type") or "").strip().lower()
        try:
            x, y, w, h = float(item["x"]), float(item["y"]), float(item["w"]), float(item["h"])
        except (KeyError, TypeError, ValueError):
            continue

        # Text is recorded, not sprited. The box still matters: it is the mask that erases
        # the lettering from whatever frame sits under it (see `_deoccluded_screenshot`),
        # once the user opts this caption into Remove or Extract (`text_mode`, off/Keep
        # by default) in the Text step.
        if raw_type == "text":
            db.add(MockupLabel(
                mockup_id=mockup.id, name=name, text=str(item.get("text") or ""),
                x=x, y=y, w=w, h=h,
                color=str(item.get("color") or "#FFFFFF")[:16],
                align=str(item.get("align") or "center")[:12],
            ))
            continue

        atype = raw_type if raw_type in ASSET_TYPES else "icon"

        # A box covering most of the screen is the scene backdrop leaking through, not UI
        # chrome — skip it regardless of name (a full-width nav bar is only ~11% area).
        if (w * h) / 10000.0 > 0.6:
            continue

        item_res = str(item.get("resolution") or "").strip()
        if not item_res or "x" not in item_res.lower():
            item_res = calculate_region_resolution(img_w, img_h, w, h)

        template = str(item.get("template") or "").strip() or None
        icon_prompt = str(item.get("icon") or "").strip() or None
        region = MockupRegion(
            mockup_id=mockup.id, name=name, x=x, y=y, w=w, h=h,
            color=REGION_COLORS_SERVER[len(mockup.regions) % len(REGION_COLORS_SERVER)],
            prompt=str(item.get("prompt") or ""), asset_type=atype,
            resolution=item_res, template=template, icon_prompt=icon_prompt,
            detect_rect=[x, y, w, h],
        )
        db.add(region)
    db.commit()
    db.refresh(mockup)
    emit.emit("done", "done", f"Created {len(mockup.regions)} regions and {len(mockup.labels)} text labels")
    return mockup


@router.post("/mockups/{mockup_id}/detect-regions", response_model=MockupOut)
def detect_regions(mockup_id: int, body: DetectRegionsBody, db: Session = Depends(get_db)):
    """Vision-detect the reusable UI elements on an uploaded screen (icons, buttons,
    panels, badges — excluding background art and hero/character renders) and create a
    MockupRegion per element. Existing unbound regions are replaced; already-generated
    (bound) regions are left alone."""
    mockup = get_mockup_or_404(db, mockup_id)
    emit = make_emitter(mockup.project_id, entity_type="mockup", entity_id=mockup.id)
    try:
        result = _detect_regions(db, mockup, body, emit)
        emit.emit("__done__", "done", "", data={"mockup_id": mockup_id})
        return result
    except HTTPException as e:
        emit.emit("__error__", "error", str(e.detail), data={"quota_exceeded": True} if is_quota_error(str(e.detail)) else None)
        raise


@router.post("/mockups/{mockup_id}/detect-regions/stream")
def detect_regions_stream(mockup_id: int, body: DetectRegionsBody):
    """SSE variant of detect-regions with live step events + the analyzed screen image."""
    with SessionLocal() as db:
        mockup = get_mockup_or_404(db, mockup_id)
        project_id = mockup.project_id

    def work(emit: Emitter):
        with SessionLocal() as db:
            mockup = get_mockup_or_404(db, mockup_id)
            _detect_regions(db, mockup, body, emit)
            return {"mockup_id": mockup_id}

    return sse_response(project_id, work, entity_type="mockup", entity_id=mockup_id)


REGION_COLORS_SERVER = ["#6c8cff", "#2dd4bf", "#c084fc", "#f5a623", "#f472b6", "#3ecf8e"]


# ---------------------------------------------------------------------------
# Subdivision — proposing how one element decomposes into sub-assets
#
# Detection answers "what is on this screen". This answers "and what is that made of",
# which is a different question with a different failure mode: a wrong box costs a bad
# crop, a wrong split costs an element that can never be reused. So nothing here mutates
# anything — `_propose_splits` only reports, and the user approves what actually happens.
# ---------------------------------------------------------------------------


class SplitChild(BaseModel):
    name: str
    asset_type: str = "icon"
    prompt: str = ""
    x: float
    y: float
    w: float
    h: float


class SplitProposal(BaseModel):
    region_id: int
    region_name: str
    # frame_icon — a container plus the one glyph drawn on it (a currency pill and its gem)
    # container  — a bar/panel plus several distinct children (a nav bar and its five icons)
    # repeat     — one box that is really N copies of the same element side by side
    kind: str
    confidence: float
    reason: str
    # `repeat` children ARE the element, so the parent box is retired in their favour.
    # The other kinds keep the parent as the empty frame the children sit on.
    replace_parent: bool = False
    # The parent already has an asset, so applying this discards that binding and the
    # parent is rebuilt as an empty frame. Surfaced so the UI can say so before it happens.
    rebuilds_parent: bool = False
    children: list[SplitChild] = []


class ProposeSplitsBody(BaseModel):
    llm: LlmSelection | None = None
    region_ids: list[int] | None = None  # default: every unbound region


class ApplySplitsBody(BaseModel):
    proposals: list[SplitProposal] = []


# A crop straight out of a screenshot is often only ~200px wide, which is below what a
# vision model can resolve a glyph in. Enlarging costs nothing and is the difference
# between "there is a gem here" and a shrug.
VISION_CROP_MIN_DIM = 512
# Elements smaller than this in either direction have nothing worth splitting out.
SPLIT_MIN_REGION_PCT = 0.8
# Bound on how many vision calls one proposal run may make.
MAX_SPLIT_CANDIDATES = 12


def _crop_for_vision(crop: Image.Image):
    """Write `crop` to a temp PNG, enlarged enough for a vision model to read it.

    Returns (path, width, height, cleanup). Boxes come back as percentages, which are
    scale-invariant, so the enlargement never has to be undone.
    """
    import os as _os
    import tempfile

    w, h = crop.size
    scale = max(1.0, VISION_CROP_MIN_DIM / max(1, max(w, h)))
    if scale > 1.0:
        crop = crop.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    fd, name = tempfile.mkstemp(suffix=".png", prefix="split-")
    _os.close(fd)
    dest = Path(name)
    crop.save(dest)
    return dest, crop.width, crop.height, (lambda: dest.unlink(missing_ok=True))


def _split_candidates(regions: list[MockupRegion], crops: dict) -> list[tuple]:
    """Which regions are worth asking a vision model about, and why.

    Cheap filters only. Three things make an element a candidate: the detector already
    said it is a template with an inner icon; its pixels repeat; or it is a UI frame big
    enough to have something drawn on it. Everything already decomposed is skipped —
    re-proposing a split the user has is just noise in the review step.

    Regions that are already BUILT are deliberately still candidates. A screen whose
    currency pill was cut whole, gem baked in, is precisely the case this feature exists
    for, and skipping bound regions made it unreachable there — the pill is only ever
    discovered to be wrong after you have seen it come out flat. Applying a split unbinds
    the parent so the next build remakes it empty; the flat asset stays in the library
    rather than being deleted, so the change is recoverable.
    """
    rects = [(r.x, r.y, r.w, r.h) for r in regions]
    out = []
    for region in regions:
        if region.w < SPLIT_MIN_REGION_PCT or region.h < SPLIT_MIN_REGION_PCT:
            continue
        rect = (region.x, region.y, region.w, region.h)
        others = [r for r in rects if r != rect]
        if has_children(rect, others):
            continue

        crop = crops.get(region.id)
        if crop is None:
            continue
        if region.template or region.icon_prompt:
            what = region.icon_prompt or "an inner icon"
            out.append((region, f"detected as a '{region.template or region.name}' template with {what}"))
            continue
        repeat = repeat_units(crop)
        if repeat is not None:
            axis, units = repeat
            out.append((region, f"its pixels repeat — this box may hold {len(units)} identical elements stacked {axis}ly"))
        elif region.asset_type == "ui_element":
            out.append((region, "a UI frame, which usually has icons or glyphs drawn on it"))
    return out[:MAX_SPLIT_CANDIDATES]


def _ask_vision_for_children(crop_path, crop_w: int, crop_h: int, region: MockupRegion,
                             hint: str, provider: str, options) -> list[dict]:
    """One vision call over a single element crop: what is drawn on top of this thing?"""
    runner = get_runner(provider)
    options.images = [crop_path]
    instruction = (
        f"Look at the image file at path: {crop_path}\n"
        f"It is ONE cropped UI element from a mobile game screen, named '{region.name}'. "
        f"Context: {hint}\n"
        "\nDecide which of these it is:\n"
        "  (A) a SINGLE indivisible piece of art — reply with an empty array []\n"
        "  (B) a CONTAINER (frame, capsule, pill, panel, bar, button) with one or more "
        "separate foreground pieces drawn on top of it — icons, glyphs, symbols, badges, "
        "portraits. Emit one item per foreground piece with \"role\": \"foreground\". Do NOT "
        "emit the container itself, and do NOT emit text, numbers or letters.\n"
        "  (C) N COPIES of the SAME element side by side that were mistakenly boxed "
        "together (e.g. two identical currency pills stacked in one box). Emit one item per "
        "copy with \"role\": \"instance\", each box covering that whole copy.\n"
        "\nBOUNDING BOXES: output `box_2d` as [ymin, xmin, ymax, xmax] normalized to 0-1000 "
        f"relative to THIS CROP ({crop_w}x{crop_h}), where 0 is the crop's top/left edge and "
        "1000 its bottom/right. Each box must tightly enclose that piece's full painted "
        "extent and nothing else.\n"
        "\nBe conservative: if the element reads as one piece of art — a solid icon, a coin, "
        "a badge, a portrait, a frame with nothing on it — reply []. Splitting something "
        "that should stay whole is worse than leaving it.\n"
        "\nReply with ONLY a valid JSON array, no prose, no markdown fences. Item format:\n"
        '{"name": "GemIcon", "type": "icon", "role": "foreground", '
        '"box_2d": [180, 40, 830, 300], "prompt": "faceted purple gem"}\n'
        "type must be one of: ui_element, icon, sprite. name must be a short PascalCase "
        "identifier."
    )
    raw = runner.run(instruction, options)
    return _parse_json_array(raw)


def _propose_splits(db: Session, mockup: Mockup, body: "ProposeSplitsBody", emit) -> dict:
    src = abs_path(mockup.image_path)
    if not src.exists():
        raise HTTPException(400, "Mockup image file missing")

    regions = list(mockup.regions)
    if body.region_ids:
        wanted = set(body.region_ids)
        regions = [r for r in regions if r.id in wanted]
    if not regions:
        emit.emit("done", "done", "No elements to analyse")
        return {"proposals": [], "mockup_id": mockup.id}

    with Image.open(src) as img:
        base = img.convert("RGB")
        W, H = base.size
        crops = {}
        for r in regions:
            box = (
                int(W * r.x / 100), int(H * r.y / 100),
                int(W * (r.x + r.w) / 100), int(H * (r.y + r.h) / 100),
            )
            if box[2] > box[0] and box[3] > box[1]:
                crops[r.id] = base.crop(box)

    candidates = _split_candidates(regions, crops)
    if not candidates:
        emit.emit("done", "done",
                  "Nothing to divide — every element is a single piece or already broken down")
        return {"proposals": [], "mockup_id": mockup.id}

    emit.emit("analyse", "running", f"Checking {len(candidates)} element(s) for sub-assets…",
              index=0, total=len(candidates))

    provider, options = resolve_options(db, body.llm)
    all_rects = [(r.x, r.y, r.w, r.h) for r in regions]
    proposals: list[dict] = []

    for idx, (region, reason) in enumerate(candidates, start=1):
        if getattr(emit, "is_stopped", False):
            break
        emit.emit("analyse", "running", f"Analysing {region.name}…", index=idx, total=len(candidates))
        crop_path, cw, ch, cleanup = _crop_for_vision(crops[region.id])
        try:
            items = _ask_vision_for_children(crop_path, cw, ch, region, reason, provider, options)
            refined = filter_and_refine_regions(crop_path, items, cw, ch)
        except LlmError as e:
            emit.emit("analyse", "error", f"{region.name}: {e}")
            continue
        finally:
            cleanup()

        parent = (region.x, region.y, region.w, region.h)
        siblings = [r for r in all_rects if r != parent]
        children = []
        instances = 0
        for item in refined:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            role = str(item.get("role") or "foreground").strip().lower()
            child = to_screen_pct(parent, item["x"], item["y"], item["w"], item["h"])
            if is_already_covered(child, siblings):
                continue
            if role == "instance":
                instances += 1
            atype = str(item.get("type") or "").strip().lower()
            children.append({
                "name": name,
                "asset_type": atype if atype in ASSET_TYPES else "icon",
                "prompt": str(item.get("prompt") or ""),
                "x": child[0], "y": child[1], "w": child[2], "h": child[3],
            })

        if not children:
            emit.emit("analyse", "done", f"{region.name} is a single piece — nothing to split",
                      index=idx, total=len(candidates))
            continue

        is_repeat = instances >= 2 and instances == len(children)
        if is_repeat:
            kind, replace = "repeat", True
            why = f"This one box holds {instances} copies of the same element — each should be its own."
        elif len(children) == 1:
            kind, replace = "frame_icon", False
            why = f"The empty frame and {children[0]['name']} can be separate, reusable assets — {reason}."
        else:
            kind, replace = "container", False
            why = f"{len(children)} pieces are drawn on this frame — {reason}."

        proposals.append({
            "region_id": region.id,
            "region_name": region.name,
            "kind": kind,
            "confidence": 0.9 if (region.template or region.icon_prompt) and kind == "frame_icon" else 0.7,
            "reason": why,
            "replace_parent": replace,
            "rebuilds_parent": region.asset_id is not None,
            "children": children,
        })
        emit.emit("analyse", "done", f"{region.name} → {len(children)} sub-asset(s)",
                  index=idx, total=len(candidates))

    emit.emit("done", "done", f"Proposed splits for {len(proposals)} of {len(candidates)} element(s)")
    return {"proposals": proposals, "mockup_id": mockup.id}


@router.post("/mockups/{mockup_id}/propose-splits")
def propose_splits(mockup_id: int, body: ProposeSplitsBody, db: Session = Depends(get_db)):
    """Report how each element could be divided into sub-assets. Mutates nothing — the
    caller reviews the proposals and posts back the approved subset to /apply-splits."""
    mockup = get_mockup_or_404(db, mockup_id)
    emit = make_emitter(mockup.project_id, entity_type="mockup", entity_id=mockup.id)
    try:
        result = _propose_splits(db, mockup, body, emit)
        emit.emit("__done__", "done", "", data=result)
        return result
    except HTTPException as e:
        emit.emit("__error__", "error", str(e.detail),
                  data={"quota_exceeded": True} if is_quota_error(str(e.detail)) else None)
        raise


@router.post("/mockups/{mockup_id}/propose-splits/stream")
def propose_splits_stream(mockup_id: int, body: ProposeSplitsBody):
    """SSE variant of propose-splits with per-element progress."""
    with SessionLocal() as db:
        mockup = get_mockup_or_404(db, mockup_id)
        project_id = mockup.project_id

    def work(emit: Emitter):
        with SessionLocal() as db:
            mockup = get_mockup_or_404(db, mockup_id)
            return _propose_splits(db, mockup, body, emit)

    return sse_response(project_id, work, entity_type="mockup", entity_id=mockup_id)


@router.post("/mockups/{mockup_id}/apply-splits", response_model=MockupOut)
def apply_splits(mockup_id: int, body: ApplySplitsBody, db: Session = Depends(get_db)):
    """Materialise approved splits as real child regions.

    Children are ordinary `MockupRegion` rows, which is the whole trick: the build already
    knows that a region enclosing smaller ones is an empty frame (`_resolve_containment`)
    and that occluders must be cut before the frames they sit on (the two-wave ordering in
    `_build_atlas`), so a gem boxed on its own gets extracted first and painted out of its
    pill automatically. Nothing downstream needs to know a split happened.
    """
    mockup = get_mockup_or_404(db, mockup_id)
    by_id = {r.id: r for r in mockup.regions}
    created = 0
    stale_assets: set[int] = set()

    for proposal in body.proposals:
        parent = by_id.get(proposal.region_id)
        if parent is None:
            continue
        for child in proposal.children:
            db.add(MockupRegion(
                mockup_id=mockup.id,
                name=child.name,
                x=child.x, y=child.y, w=child.w, h=child.h,
                color=REGION_COLORS_SERVER[(len(mockup.regions) + created) % len(REGION_COLORS_SERVER)],
                prompt=child.prompt,
                asset_type=child.asset_type if child.asset_type in ASSET_TYPES else "icon",
                resolution=calculate_region_resolution(*_mockup_size(mockup), child.w, child.h),
                # Extraction always segments from `detect_rect`, never from a rect a previous
                # build grew. Seed it with the child's own box so the first cut starts here.
                detect_rect=[child.x, child.y, child.w, child.h],
            ))
            created += 1
        if proposal.replace_parent and proposal.children:
            # The children ARE this element; the box around all of them was the mistake.
            if parent.asset_id is not None:
                stale_assets.add(parent.asset_id)
            db.delete(parent)
        elif proposal.children:
            # The parent is now the empty frame its children sit on. Clearing `icon_prompt`
            # is what tells `_resolve_containment` the glyph is a real region now, so the
            # child survives and the parent is built empty. `template` is deliberately kept:
            # it only drives generate-mode family grouping, which is still correct.
            parent.icon_prompt = None
            # Whatever it was built as had the children painted into it — that is the
            # defect being corrected — so release the binding and let the next build cut
            # it again, empty this time.
            if parent.asset_id is not None:
                stale_assets.add(parent.asset_id)
                parent.asset_id = None
                parent.icon_asset_id = None
    db.commit()

    for asset_id in stale_assets:
        _retire_split_asset(db, asset_id)
    db.commit()
    db.refresh(mockup)
    return mockup


def _retire_split_asset(db: Session, asset_id: int) -> None:
    """Drop the flat asset a just-split region was bound to.

    Unbinding the region is not enough on its own. The stale asset still carries the
    region's name, so `_build_atlas`'s reuse-by-name — and especially its fuzzy
    slug-containment fallback — rebinds the region to it on the very next build and the
    split silently does nothing. That is not hypothetical; it is what happened the first
    time this ran end to end.

    Deleting it is safe in a way deleting assets generally is not: it is the frame with
    its children painted into it, which is the exact defect the split exists to correct,
    and it is pure derived data — the next build cuts a correct one from the same
    screenshot. Kept if any other region still points at it, so splitting on one screen
    can never break another.
    """
    still_used = db.scalars(
        select(MockupRegion).where(
            (MockupRegion.asset_id == asset_id) | (MockupRegion.icon_asset_id == asset_id)
        )
    ).first()
    if still_used is not None:
        return
    asset = db.get(Asset, asset_id)
    if asset is not None:
        _delete_asset_files(db, asset)
        db.delete(asset)


def _mockup_size(mockup: Mockup) -> tuple[int, int]:
    src = abs_path(mockup.image_path)
    if src.exists():
        with Image.open(src) as img:
            return img.width, img.height
    return 1080, 1920


class BuildAtlasBody(BaseModel):
    atlas_id: int
    provider: str = "antigravity"
    resolution: str | None = None  # optional target resolution override; if unset, uses each region's own resolution
    # How to produce each element:
    #   extract  — cut it out of the screenshot (default; free, pixel-faithful)
    #   generate — redraw it with the image provider (drifts from the reference, costs quota)
    #   hybrid   — extract everything, then regenerate only what scored below `min_score`
    mode: str = "extract"
    min_score: float = 70.0  # hybrid: regenerate anything scoring under this
    # When True, clear all existing region→asset bindings so every element is
    # rebuilt from scratch (used by the "Rebuild all elements" button).
    rebuild: bool = False
    # Region id (as a JSON string key) → the asset the user approved for it.
    #
    # Binding a region to an asset that already exists is a decision about *this* screen,
    # and a name collision is not consent to it. So a dict here — including an empty one —
    # means "the matches were shown and decided": only the listed regions reuse an asset,
    # every other region is built even if its name matches something in the library. The
    # UI always sends a dict, after /reuse-candidates. Omitting the field entirely is the
    # script-facing escape hatch that keeps the old bind-every-match behaviour.
    reuse: dict[str, int] | None = None


class PolishRegionsBody(BaseModel):
    """Body for the standalone Polish step, which runs after Build (and after Text, if
    that step was used). Unlike `BuildAtlasBody.polish` (retired), this targets an
    explicit set of already-built regions rather than "whatever a build run just
    produced" — a user can polish one element or every element, independent of when
    each was built. Purely cosmetic (upscale + clean edges): text handling is the
    Text step's job, not this one — see `ApplyTextBody`."""
    provider: str = "antigravity"
    # None/omitted = every already-built element on the mockup. Explicit ids = just those
    # (one element, or a hand-picked subset), for polishing a single result without
    # re-touching everything else.
    region_ids: list[int] | None = None
    # None = the provider's saved default. Named so this step's model can be chosen (and
    # measured) independently of the one used to Build — redrawing an existing element is
    # a different job from generating one from scratch, and the best model for one is not
    # necessarily the best for the other.
    model: str | None = None
    # Provider-native per-model knobs (Higgsfield `--resolution`/`--quality`/
    # `--aspect_ratio`); see ImageProvider.generate.
    params: dict | None = None
    # Wording of the reference-mode prompt — see prompting.REFERENCE_VARIANTS.
    prompt_variant: str = "v1"
    # False (the default) resumes: an element that already carries a polish made from its
    # current build is left alone instead of being paid for and redrawn again — what a
    # re-run after a run that died halfway wants. True redoes everything named, for when a
    # second pass IS the intent (the per-element Polish button, or the redo toggle).
    force: bool = False


class ApplyTextBody(BaseModel):
    """Body for the Text step's Apply action, which runs after Build (and before
    Polish). Redraws exactly the regions named — or, if none are named, every built
    region with a pending Remove/Extract choice — stripping or isolating their lettering
    per each region's own choice. A region left on "Keep" costs nothing even if it's
    swept up by an unfiltered "apply to all"."""
    provider: str = "antigravity"
    region_ids: list[int] | None = None
    # Same three knobs as PolishRegionsBody, and separate from it on purpose: erasing
    # lettering and isolating it are different jobs from a cosmetic polish, so the best
    # model/wording for one need not be the best for the other.
    model: str | None = None
    params: dict | None = None
    prompt_variant: str = "v1"
    # Same resume/redo switch as PolishRegionsBody: by default a region whose lettering was
    # already stripped (and whose Extract captions already have their sprites) is skipped,
    # so re-running after a failure only buys the captions that are actually missing.
    force: bool = False


# Below this fidelity score an extracted element is judged a failure worth spending a
# generation call on. Extraction typically lands 75-97 on clean UI chrome, so 70 catches
# genuine failures without churning through quota on merely-imperfect cuts.
HYBRID_DEFAULT_MIN_SCORE = 70.0


DUP_HASH_THRESHOLD = 6  # max perceptual-hash bit distance for two crops to count as identical


def _group_by_appearance(
    mockup: Mockup, regions: list[MockupRegion]
) -> tuple[list[list[MockupRegion]], dict[int, bool]]:
    """Cluster regions whose crops are visually identical (same asset type + close
    perceptual hash) so each distinct look is generated once. A shared button background
    dropped in several spots collapses into a single group; source order is preserved.

    Also catches horizontal-mirror duplicates (e.g. a "next"/"previous" arrow pair that are
    the same artwork flipped) by additionally hashing each crop mirrored and matching that
    against the group's representative hash. These don't get a second generation call at
    all — they're bound to the SAME asset and flipped at render time (see
    `render_preview_screen`), so a rotated/mirrored repeat of an asset never burns a
    generation. Returns (groups, mirror_of) where mirror_of[region_id] says whether that
    region should be flipped relative to its group's first (representative) region."""
    src = abs_path(mockup.image_path)
    hashes: dict[int, int | None] = {}
    mirror_hashes: dict[int, int | None] = {}
    if src.exists():
        with Image.open(src) as img:
            W, H = img.size
            base = img.convert("RGB")
            for r in regions:
                box = (
                    int(W * r.x / 100), int(H * r.y / 100),
                    int(W * (r.x + r.w) / 100), int(H * (r.y + r.h) / 100),
                )
                if box[2] > box[0] and box[3] > box[1]:
                    crop = base.crop(box)
                    hashes[r.id] = perceptual_hash(crop)
                    mirror_hashes[r.id] = perceptual_hash(ImageOps.mirror(crop))

    groups: list[dict] = []
    mirror_of: dict[int, bool] = {}
    for r in regions:
        h = hashes.get(r.id)
        mh = mirror_hashes.get(r.id)
        placed = False
        if h is not None:
            for g in groups:
                if g["type"] != r.asset_type or g["hash"] is None:
                    continue
                if hamming(h, g["hash"]) <= DUP_HASH_THRESHOLD:
                    g["regions"].append(r)
                    mirror_of[r.id] = False
                    placed = True
                    break
                if mh is not None and hamming(mh, g["hash"]) <= DUP_HASH_THRESHOLD:
                    g["regions"].append(r)
                    mirror_of[r.id] = True
                    placed = True
                    break
        if not placed:
            groups.append({"type": r.asset_type, "hash": h, "regions": [r]})
            mirror_of[r.id] = False
    return [g["regions"] for g in groups], mirror_of


def _make_asset(
    db: Session, project: Project, atlas_id: int | None, name: str, atype: str,
    prompt_text: str, references: list, resolution: str, aspect: str, is_sliced: bool,
    provider_name: str, emit,
) -> Asset:
    """Generate one standalone asset (image + Asset + AssetVersion) from a prompt and
    reference images, running the same post-processing (bg removal, trim, fit) as a
    region asset. Used to mint the shared background and the per-instance icons of a
    template family."""
    try:
        provider = get_enabled_provider(provider_name)
    except ProviderError as e:
        raise HTTPException(403, str(e))
    model = resolve_model(provider_name)
    nine = {"l": 32, "t": 32, "r": 32, "b": 32} if is_sliced else None
    asset = Asset(
        project_id=project.id, atlas_id=atlas_id, name=name, type=atype,
        prompt=prompt_text, aspect_ratio=aspect, resolution=resolution, nine_slice=nine,
    )
    db.add(asset)
    db.commit()
    full_prompt = compose_prompt(project, asset)
    raw = new_asset_path(db, asset, f"{name}-raw")
    ref_paths = [rel_path(r) for r in (references or []) if Path(r).exists()]
    toks = estimate_tokens(full_prompt, output="", image_count=len(references or []))
    make_data = {
        "prompt": full_prompt,
        "provider": provider.name,
        "model": model,
        "reference_images": ref_paths,
        "tokens": toks,
    }
    emit.emit("generate", "running", f"Generating {name} · {provider.name}/{model or 'default'}…", data=make_data)
    try:
        provider.generate(full_prompt, raw, reference_images=references, transparent=True, model=model)
    except ProviderError as e:
        msg = str(e)
        emit.emit("generate", "error", msg, data={"quota_exceeded": True} if is_quota_error(msg) else make_data)
        if asset and asset.selected_version_id is None:
            _delete_asset_files(db, asset)
            db.delete(asset)
            db.commit()
        raise HTTPException(502, msg)

    cmd_str = getattr(provider, "last_command", None)
    if cmd_str:
        make_data["command"] = cmd_str
    emit.emit("generate", "done", f"Raw image generated for {name}", data=make_data)
    processed = new_asset_path(db, asset, name)
    with Image.open(raw) as img:
        out = remove_background(img) if provider.needs_transparency_postprocess else img.convert("RGBA")
        out = trim_for_fit(out, nine is not None, aspect)
        out = fit_to_resolution(out, resolution)
        done_make_data = {
            **make_data,
            "tokens": estimate_tokens(full_prompt, output=f"Generated {name} PNG", image_count=len(references or [])),
        }
        emit.emit("asset", "done", f"{name} → {resolution}", image=emit.preview(out, "final"), data=done_make_data)
        out.save(processed)
    version = AssetVersion(
        asset_id=asset.id, provider=provider.name, model=model, composed_prompt=full_prompt,
        raw_path=rel_path(raw), processed_path=rel_path(processed),
        reference_paths=ref_paths,
    )
    db.add(version)
    db.commit()
    asset.selected_version_id = version.id
    if nine is not None:
        with Image.open(abs_path(version.processed_path)) as im:
            asset.nine_slice = detect_borders(im)
    db.commit()
    db.refresh(asset)
    return asset


def _build_template_family(
    db: Session, project: Project, atlas: Atlas, members: list[MockupRegion],
    provider_name: str, emit,
) -> list[dict]:
    """Generate a shared background ONCE for a group of same-template regions, and generate
    each member's distinguishing icon as its own asset. No combined sprite is baked: every
    member region binds to the SAME background asset (`asset_id`) plus its own icon asset
    (`icon_asset_id`), and the preview/export layer the two at render time. So the atlas
    stores one reusable frame + N icons, and every member is guaranteed identical bar its
    icon because they literally share the one background image."""
    template = members[0].template or "Template"
    ref_crop = abs_path(_crop_region(db, members[0]))
    proj_refs = _project_refs(project)

    bg_name = f"{template}Background"
    existing_bg = db.scalars(
        select(Asset).where(
            Asset.project_id == project.id,
            Asset.name == bg_name,
            Asset.selected_version_id != None,
        )
    ).first()

    if existing_bg:
        bg_asset = existing_bg
        emit.emit("template", "done", f"Reused existing {bg_name}")
    else:
        bg_prompt = (
            f"Extract ONLY the EMPTY {template} frame/background from the reference image: "
            "the capsule/panel/pill shape with its border, material and colors exactly as in the reference crop, "
            "isolated without its original background and with a COMPLETELY BLANK interior — "
            "no icon, no number, no text, no symbol inside, nothing but the empty frame. "
            "The borders MUST be perfectly symmetric on all four sides (left = right, top = bottom, all corners identical). "
            "All edges must be sharp, clean, and crisp."
        )
        bg_asset = _make_asset(
            db, project, atlas.id, bg_name, "ui_element", bg_prompt,
            [ref_crop] + proj_refs, members[0].resolution or DEFAULT_RESOLUTION,
            ratio_string(members[0].w, members[0].h), True, provider_name, emit,
        )

    results = []

    def _icon_worker(r: MockupRegion):
        if getattr(emit, "is_stopped", False):
            return None
        icon_desc = (r.icon_prompt or r.name).strip()
        icon_name = f"{r.name}Icon"
        with SessionLocal() as thread_db:
            thread_r = thread_db.get(MockupRegion, r.id)
            if not thread_r:
                return None
            crop = abs_path(_crop_region(thread_db, thread_r))
            existing_icon = thread_db.scalars(
                select(Asset).where(
                    Asset.project_id == project.id,
                    Asset.name == icon_name,
                    Asset.selected_version_id != None,
                )
            ).first()

            if existing_icon:
                icon_asset = existing_icon
            else:
                icon_prompt = (
                    f"Extract the single {icon_desc} icon ONLY from the reference image — take just the icon symbol itself, "
                    "isolated and centered, with NO surrounding frame, capsule, pill, panel, number or text, and with no original background. "
                    "Match the icon shown in the reference image exactly."
                )
                icon_asset = _make_asset(
                    thread_db, project, atlas.id, icon_name, "icon", icon_prompt,
                    [crop] + proj_refs, "256x256", "1:1", False, provider_name, emit,
                )
            thread_r.asset_id = bg_asset.id          # shared frame (same asset for every member)
            thread_r.icon_asset_id = icon_asset.id   # this member's distinguishing icon
            thread_db.commit()
            emit.emit("template", "done", f"{thread_r.name} = {template} frame + {icon_desc}")
            return {
                "region_id": thread_r.id, "asset_id": bg_asset.id,
                "icon_asset_id": icon_asset.id, "reused": False,
            }

    max_workers = min(3, max(1, len(members)))
    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_icon_worker, r) for r in members]
            for future in as_completed(futures):
                if getattr(emit, "is_stopped", False):
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                res = future.result()
                if res:
                    results.append(res)
    else:
        for r in members:
            if getattr(emit, "is_stopped", False):
                break
            res = _icon_worker(r)
            if res:
                results.append(res)

    return results


def _resolve_containment(regions: list[MockupRegion]) -> tuple[set[int], set[int]]:
    """Classify regions by geometric nesting (using their stored percentage rects, so this
    reflects any boxes the user hand-edited too):
      • background ids — a region that fully encloses ≥1 smaller region is a frame/panel/bar
        that foreground elements sit on; it is generated as an EMPTY frame and the enclosed
        elements become their own foreground sprites layered on top.
      • drop ids — a foreground box that falls inside a TEMPLATE member (a currency pill etc.)
        is already represented by that template's `icon` field, so a separate box for it would
        double-generate the glyph; drop it. This only holds while the glyph is *just* a
        description: once a split has given it a real region, the parent's `icon_prompt` is
        cleared and the parent becomes an ordinary container whose child must survive.

    The two use different thresholds because they answer different questions. Being *drawn
    on* only needs overlap — a badge hanging off a banner's corner still has to be painted
    out of it, and demanding containment is what left the profile banner with the avatar
    and the level badge baked in. Being *owned by a template* is a claim that the glyph is
    fully accounted for elsewhere, so that one still demands real containment.

    Extracted text regions take no part in this — see `_buildable_regions`. Their pixels
    were already lifted out of the parent by the parent's own build, so counting them as
    things sitting on it would have the frame erase its caption twice and would mark any
    element with an extracted caption as a background whether or not it is one.
    """
    COVER, SHRINK = 0.80, 0.85
    regions = [r for r in regions if r.source != TEXT_SOURCE]
    bg_ids: set[int] = set()
    drop_ids: set[int] = set()
    for outer in regions:
        oarea = outer.w * outer.h
        for inner in regions:
            if inner.id == outer.id or inner.w * inner.h >= SHRINK * oarea:
                continue
            overlap = containment_ratio(
                (inner.x, inner.y, inner.w, inner.h),
                (outer.x, outer.y, outer.w, outer.h),
            )
            # A template only *owns* its glyph while that glyph is still just a description.
            # Once a split has given the icon a real box of its own, `icon_prompt` is cleared
            # and the templated region is an ordinary container: its child must survive
            # `drop_ids` (or the next build deletes the sub-asset the user just approved),
            # and the parent must be built as an empty frame like any other background.
            if outer.template and outer.icon_prompt and overlap >= COVER:
                drop_ids.add(inner.id)  # glyph inside a templated pill: template owns it
            elif overlap >= OCCLUDE_MIN_OVERLAP:
                bg_ids.add(outer.id)
    return bg_ids, drop_ids


def render_preview_screen(db: Session, mockup: Mockup, bg: str | None = None) -> dict:
    """Composite every bound asset into a screen-sized image. `bg` defaults to None,
    which draws a checkerboard behind the elements — right for the UI preview, wrong for
    scoring (the checkerboard's grey would be measured against the game art), so the
    scorer passes a fully transparent "#00000000"."""
    project = get_project_or_404(db, mockup.project_id)
    img_path = abs_path(mockup.image_path)
    if not img_path.exists():
        raise HTTPException(400, "Mockup image file missing")
    with Image.open(img_path) as src:
        W, H = src.size

    def _asset_and_image(asset_id: int | None):
        asset = db.get(Asset, asset_id) if asset_id else None
        if not asset:
            return None, None
        if asset in db:
            db.expire(asset)
        if not asset.selected_version_id:
            return None, None
        version = next((v for v in asset.versions if v.id == asset.selected_version_id), None)
        if not version:
            return None, None
        p = abs_path(version.processed_path or version.raw_path)
        if not p.exists():
            return None, None
        with Image.open(p) as im:
            return asset, im.copy()

    elements = []
    missing = []
    for region in mockup.regions:
        bg_asset, bg_img = _asset_and_image(region.asset_id)
        if bg_img is None:
            missing.append(region.name)
            continue
        if region.mirror:
            bg_img = ImageOps.mirror(bg_img)
        box = {"x": region.x / 100, "y": region.y / 100, "w": region.w / 100, "h": region.h / 100}
        fit_mode = fit_mode_for(bg_asset.type, bg_asset.nine_slice)
        elements.append({
            "image": bg_img,
            "rect": box,
            "z": -(region.w * region.h),
            "fit": fit_mode,
            "nine_slice": bg_asset.nine_slice if fit_mode == "slice" else None,
        })
        _, icon_img = _asset_and_image(region.icon_asset_id) if region.icon_asset_id else (None, None)
        if icon_img is not None and region.mirror:
            icon_img = ImageOps.mirror(icon_img)
        if icon_img is not None:
            box_h_px, box_w_px = box["h"] * H, box["w"] * W
            ih = 0.78 * box_h_px
            iw = ih * icon_img.width / icon_img.height if icon_img.height else ih
            elements.append({
                "image": icon_img,
                "rect": {
                    "x": box["x"] + (0.12 * box_h_px) / W,
                    "y": box["y"] + ((box_h_px - ih) / 2) / H,
                    "w": iw / W,
                    "h": ih / H,
                },
                "z": 1e12,
                "fit": "contain",
            })

    canvas = composite_layout(elements, W, H, bg=bg)
    dest = new_image_path(project.id, "previews", "screen-preview")
    canvas.save(dest)
    return {"path": rel_path(dest), "missing": missing}


def _reusable_assets(db: Session, atlas: Atlas) -> dict[tuple[str, str], Asset]:
    """The assets a region could be bound to instead of being built: everything visible
    from this domain, keyed by (name slug, type).

    Extracted lettering is not a spare part. It is owned by the element it came off and
    re-cut whenever that element is, so it must not be offered to some other region by the
    reuse match — whose fuzzy fallback matches on slug containment, and
    "playbutton-text-play" contains "play".
    """
    text_asset_ids = {
        r.asset_id for r in db.scalars(
            select(MockupRegion).where(MockupRegion.source == TEXT_SOURCE)
        ) if r.asset_id
    }
    return {
        (slugify(a.name), a.type): a
        for a in available_assets(db, atlas) if a.id not in text_asset_ids
    }


def _reuse_match(
    region: MockupRegion, reusable: dict[tuple[str, str], Asset],
) -> tuple[Asset, str] | None:
    """The asset this region would reuse rather than build, and how it was found: "exact"
    on name slug + type, else "fuzzy" on the two slugs containing one another.

    A proposal only — nothing here binds it. `reuse_candidates` shows these to the user and
    `_build_atlas` binds the ones that come back approved in `BuildAtlasBody.reuse`."""
    slug_r = slugify(region.name)
    exact = reusable.get((slug_r, region.asset_type))
    if exact:
        return exact, "exact"
    for a in reusable.values():
        if a.type == region.asset_type:
            slug_a = slugify(a.name)
            if slug_r and slug_a and (slug_r in slug_a or slug_a in slug_r):
                return a, "fuzzy"
    return None


def _asset_image_path(asset: Asset) -> str | None:
    """Storage-relative image of an asset's selected version, for a thumbnail."""
    version = next((v for v in asset.versions if v.id == asset.selected_version_id), None)
    return (version.processed_path or version.raw_path) if version else None


def _build_atlas(db: Session, mockup: Mockup, body: "BuildAtlasBody", emit) -> dict:
    project = get_project_or_404(db, mockup.project_id)
    atlas = get_atlas_or_404(db, body.atlas_id)
    if atlas.project_id != project.id:
        raise HTTPException(400, "Atlas belongs to a different project")

    reusable = _reusable_assets(db, atlas)

    # Which regions may reuse an existing asset, per the caller's approvals. None means the
    # caller never asked (a script), so the name heuristic decides on its own; a dict means
    # the user was shown each match and answered, so nothing outside it is bound.
    approvals: dict[int, int] | None = None
    if body.reuse is not None:
        available_ids = {a.id for a in reusable.values()}
        approvals = {}
        for key, asset_id in body.reuse.items():
            try:
                region_id = int(key)
            except (TypeError, ValueError):
                raise HTTPException(400, f"reuse: {key!r} is not a region id")
            if asset_id not in available_ids:
                raise HTTPException(
                    400, f"reuse: asset {asset_id} is not available from this domain",
                )
            approvals[region_id] = asset_id

    # Decompose composite elements: which regions are empty-frame backgrounds, and which
    # stray glyph boxes a currency-pill template already covers (drop those).
    bg_ids, drop_ids = _resolve_containment(list(mockup.regions))
    for r in list(mockup.regions):
        if r.id in drop_ids and r.asset_id is None:
            emit.emit("dedup", "done", f"Dropped {r.name} (covered by its template icon)")
            db.delete(r)
    if drop_ids:
        db.commit()
        db.refresh(mockup)

    results, errors = [], []
    to_generate: list[MockupRegion] = []

    # Rebuild mode: unbind every region so the loop below treats them all as pending.
    # Except the extracted text sprites — those are re-cut by the element they came off, as
    # part of that element's own rebuild, so unbinding them here would send them through
    # segmentation as if they were elements in their own right.
    if body.rebuild:
        for region in _buildable_regions(mockup):
            if region.asset_id:
                region.asset_id = None
        db.commit()
        db.refresh(mockup)

    for region in _buildable_regions(mockup):
        if region.asset_id:
            existing = db.get(Asset, region.asset_id)
            if existing:
                results.append({"region_id": region.id, "asset_id": region.asset_id, "reused": False})
                continue
            else:
                region.asset_id = None
                db.commit()
        if approvals is None:
            # A region freshly unbound by a hand-edit must not silently rematch its own old
            # asset by name — that asset is exactly what the edit invalidated (see
            # force_rebuild's model comment). Send it straight to extraction instead.
            found = (
                None if body.rebuild or region.force_rebuild
                else _reuse_match(region, reusable)
            )
            match = found[0] if found else None
        else:
            # An approval outranks both the name heuristic and force_rebuild: the user was
            # shown this asset next to this region and said to use it here.
            match = db.get(Asset, approvals[region.id]) if region.id in approvals else None

        if match:
            region.asset_id = match.id
            db.commit()
            emit.emit("reuse", "done", f"Reused existing {region.name}")
            results.append({"region_id": region.id, "asset_id": match.id, "reused": True})
            try:
                db.expire(mockup)
                prev_data = render_preview_screen(db, mockup)
                emit.emit("preview", "done", f"Updated preview ({region.name})", data=prev_data)
            except Exception:
                pass
            continue
        to_generate.append(region)

    # Shared-background templates first: regions the vision model tagged as the same
    # repeated component (2+ members) get one generated frame + per-member icons composited
    # on top, so they come out pixel-identical bar the icon. Lone templated regions fall
    # through to normal generation.
    # Only meaningful when redrawing. Sharing one generated frame across members is what
    # stops siblings drifting apart — but extraction can't drift, since each member's
    # frame is literally its own pixels, and forcing them onto a shared frame would throw
    # away real per-instance detail. Extracted duplicates are still collapsed by the
    # perceptual-hash grouping below.
    families: dict[str, list[MockupRegion]] = {}
    plain: list[MockupRegion] = []
    generating = (body.mode or "extract").lower() == "generate"
    for r in to_generate:
        if r.template and generating and (r.source or "generate") == "generate":
            families.setdefault(r.template, []).append(r)
        else:
            plain.append(r)

    # Families with only one member don't get a shared frame — fold them back into the
    # plain pool now, before anything is built, so the appearance grouping below (and thus
    # the step count) reflects the real work rather than growing once building starts.
    family_items = [(t, m) for t, m in families.items() if len(m) >= 2]
    for template, members in families.items():
        if len(members) < 2:
            plain.extend(members)

    # Collapse visually-identical (and mirror-duplicate) regions so each distinct look is
    # generated exactly once, then bind every region in the group to that single asset.
    groups, mirror_of = _group_by_appearance(mockup, plain)

    # Every unit of remaining work — one per shared-template family, one per appearance
    # group — is known here, before a single one of them has run. Emitting index/total off
    # this fixed count (instead of a total that grows as more work is discovered) is what
    # lets the UI draw an accurate 0-100 bar rather than one that jumps around.
    total = len(family_items) + len(groups)
    done_idx = 0
    preview_lock = threading.Lock()
    if total > 0:
        emit.emit("plan", "done", f"{total} step{'s' if total != 1 else ''} to build", index=0, total=total)

    for template, members in family_items:
        if getattr(emit, "is_stopped", False):
            break
        done_idx += 1
        emit.emit("template", "running", f"Shared frame for {template} (+{len(members)} icons)", index=done_idx, total=total)
        try:
            family_res = _build_template_family(db, project, atlas, members, body.provider, emit)
            results.extend(family_res)
            emit.emit("template", "done", f"Built shared frame for {template}", index=done_idx, total=total)
            try:
                db.expire(mockup)
                prev_data = render_preview_screen(db, mockup)
                emit.emit("preview", "done", f"Updated preview ({template})", data=prev_data)
            except Exception:
                pass
        except HTTPException as e:
            if is_quota_error(e.detail):
                emit.emit(
                    "template", "error", f"Stopped — provider quota/rate limit reached: {e.detail}",
                    index=done_idx, total=total, data={"quota_exceeded": True},
                )
                emit.stop()
            else:
                emit.emit("template", "error", f"{template}: {e.detail}", index=done_idx, total=total)
            for r in members:
                errors.append({"region_id": r.id, "name": r.name, "error": e.detail})

    def _worker(group_args):
        done_idx, group = group_args
        if getattr(emit, "is_stopped", False):
            return None
        rep = group[0]
        extra = f" (+{len(group) - 1} identical)" if len(group) > 1 else ""
        # A per-region `source` override beats the run's mode, so one stubborn element can
        # be forced down the other path without changing how everything else is built.
        want = (rep.source or body.mode or "extract").lower()
        verb = "Extracting" if want != "generate" else "Generating"
        emit.emit("region", "running", f"{verb} {rep.name}{extra}", index=done_idx, total=total)

        res_items = []
        err_items = []
        with SessionLocal() as thread_db:
            try:
                thread_rep = thread_db.get(MockupRegion, rep.id)
                if not thread_rep:
                    return None
                is_bg = thread_rep.id in bg_ids
                if want == "generate":
                    asset = _generate_asset_for_region(
                        thread_db, thread_rep, body.provider, atlas_id=atlas.id,
                        resolution=body.resolution, emit=emit, background=is_bg,
                    )
                else:
                    asset = _extract_asset_for_region(
                        thread_db, thread_rep, atlas_id=atlas.id,
                        resolution=body.resolution, emit=emit, background=is_bg,
                    )
                    # Hybrid: spend a generation call only where extraction genuinely
                    # failed, judged by the same metric the whole pipeline is tuned on.
                    if want == "hybrid":
                        version = asset.selected_version
                        got = (version.fidelity or {}).get("score") if version else None
                        if got is not None and got < body.min_score:
                            emit.emit(
                                "region", "running",
                                f"{thread_rep.name} extracted at {got:.0f} — below {body.min_score:.0f}, regenerating",
                                index=done_idx, total=total,
                            )
                            asset = _generate_asset_for_region(
                                thread_db, thread_rep, body.provider, atlas_id=atlas.id,
                                resolution=body.resolution, emit=emit, background=is_bg,
                            )
                thread_rep.mirror = False
                thread_rep.force_rebuild = False
                thread_db.commit()
                res_items.append({"region_id": thread_rep.id, "asset_id": asset.id, "reused": False})
                emit.emit("region", "done", f"Built {thread_rep.name}{extra}", index=done_idx, total=total)
                for r in group[1:]:
                    thread_r = thread_db.get(MockupRegion, r.id)
                    if thread_r:
                        thread_r.asset_id = asset.id
                        thread_r.mirror = mirror_of.get(r.id, False)
                        thread_r.force_rebuild = False
                        thread_db.commit()
                        note = " (mirrored)" if thread_r.mirror else ""
                        emit.emit("reuse", "done", f"Reused {thread_rep.name} for {thread_r.name}{note}", index=done_idx, total=total)
                        res_items.append({"region_id": thread_r.id, "asset_id": asset.id, "reused": True, "mirror": thread_r.mirror})
                with preview_lock:
                    try:
                        thread_mockup = thread_db.get(Mockup, mockup.id)
                        if thread_mockup:
                            thread_db.expire(thread_mockup)
                        prev_data = render_preview_screen(thread_db, thread_mockup or mockup)
                        emit.emit("preview", "done", f"Updated preview ({thread_rep.name})", data=prev_data)
                    except Exception:
                        pass
            except HTTPException as e:
                if is_quota_error(e.detail):
                    emit.emit(
                        "region", "error",
                        f"Stopped — provider quota/rate limit reached: {e.detail}",
                        index=done_idx, total=total, data={"quota_exceeded": True},
                    )
                    emit.stop()
                else:
                    emit.emit("region", "error", f"{rep.name}: {e.detail}", index=done_idx, total=total)
                for r in group:
                    err_items.append({"region_id": r.id, "name": r.name, "error": e.detail})
        return res_items, err_items

    # Occluders before the frames they sit on, in two waves.
    #
    # De-occlusion erases each occluder's own extracted silhouette from the screenshot
    # (`_occluder_masks`), so an icon must already have a sprite by the time its parent is
    # built — otherwise the parent falls back to erasing bounding boxes and the frame's
    # edge comes back with square bites in it. Everything inside a wave is still built
    # concurrently; only the boundary between them is ordered.
    numbered = list(enumerate(groups, start=len(family_items) + 1))
    waves = [
        [g for g in numbered if g[1][0].id not in bg_ids],
        [g for g in numbered if g[1][0].id in bg_ids],
    ]

    def _collect(out):
        if out:
            res_items, err_items = out
            results.extend(res_items)
            errors.extend(err_items)

    for wave in waves:
        if not wave or getattr(emit, "is_stopped", False):
            continue
        max_workers = min(3, max(1, len(wave)))
        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_worker, item) for item in wave]
                for future in as_completed(futures):
                    if getattr(emit, "is_stopped", False):
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    _collect(future.result())
        else:
            for item in wave:
                if getattr(emit, "is_stopped", False):
                    break
                _collect(_worker(item))

    emit.emit("done", "done", f"Built {len(results)} elements ({len(errors)} errors)")
    return {"results": results, "errors": errors, "mockup_id": mockup.id}


class ReuseCandidatesBody(BaseModel):
    atlas_id: int
    # Mirrors BuildAtlasBody.rebuild. A rebuild run is an explicit "cut these out of the
    # screenshot again", so it proposes no reuse and this comes back empty.
    rebuild: bool = False


@router.post("/mockups/{mockup_id}/reuse-candidates")
def reuse_candidates(mockup_id: int, body: ReuseCandidatesBody, db: Session = Depends(get_db)):
    """Every region a build would bind to an asset that already exists, paired with the
    asset it would pick, how sure that match is, and the other assets of the same type in
    this domain it could be pointed at instead.

    Writes nothing. Build only reuses what its `reuse` map lists, so this is the approval
    step that goes in front of it: the user sees the region's own pixels next to the asset
    being proposed for it and decides, rather than finding out afterwards."""
    mockup = get_mockup_or_404(db, mockup_id)
    atlas = get_atlas_or_404(db, body.atlas_id)
    if atlas.project_id != mockup.project_id:
        raise HTTPException(400, "Atlas belongs to a different project")

    reusable = _reusable_assets(db, atlas)
    by_type: dict[str, list[Asset]] = {}
    for a in reusable.values():
        by_type.setdefault(a.type, []).append(a)

    candidates = []
    if not body.rebuild:
        for region in _buildable_regions(mockup):
            # Already bound, or invalidated by a hand-edit: build never proposes either.
            if region.asset_id or region.force_rebuild:
                continue
            found = _reuse_match(region, reusable)
            if not found:
                continue
            asset, how = found
            try:
                crop = _crop_region(db, region)
            except HTTPException:
                crop = None
            candidates.append({
                "region_id": region.id,
                "region_name": region.name,
                "asset_type": region.asset_type,
                "region_crop": crop,
                "asset_id": asset.id,
                "asset_name": asset.name,
                "asset_path": _asset_image_path(asset),
                "match": how,
                "options": [
                    {"id": a.id, "name": a.name, "path": _asset_image_path(a)}
                    for a in sorted(by_type.get(region.asset_type, []), key=lambda a: a.name)
                ],
            })
    return {"candidates": candidates, "mockup_id": mockup.id}


@router.post("/mockups/{mockup_id}/build-atlas")
def build_atlas(mockup_id: int, body: BuildAtlasBody, db: Session = Depends(get_db)):
    """For every region not yet bound to an asset: reuse the asset the caller approved for
    it in `body.reuse` (see /reuse-candidates), else build a new one into this domain.
    Returns the regions with their resolved asset ids."""
    mockup = get_mockup_or_404(db, mockup_id)
    emit = make_emitter(mockup.project_id, entity_type="mockup", entity_id=mockup.id)
    try:
        result = _build_atlas(db, mockup, body, emit)
        emit.emit("__done__", "done", "", data=result)
        return result
    except HTTPException as e:
        emit.emit("__error__", "error", str(e.detail), data={"quota_exceeded": True} if is_quota_error(str(e.detail)) else None)
        raise


@router.post("/mockups/{mockup_id}/build-atlas/stream")
def build_atlas_stream(mockup_id: int, body: BuildAtlasBody):
    """SSE variant of build-atlas: emits per-region progress (index/total) and each
    generated asset thumbnail as it's produced."""
    with SessionLocal() as db:
        mockup = get_mockup_or_404(db, mockup_id)
        project_id = mockup.project_id

    def work(emit: Emitter):
        with SessionLocal() as db:
            mockup = get_mockup_or_404(db, mockup_id)
            return _build_atlas(db, mockup, body, emit)

    return sse_response(project_id, work, entity_type="mockup", entity_id=mockup_id)


def _resolve_target_regions(mockup: Mockup, region_ids: list[int] | None) -> list[MockupRegion]:
    """None/omitted `region_ids` means every region on the mockup; explicit ids narrow
    that to just the named ones. Shared by Polish and Text-apply so "run against
    everything" and "run against just this one element" mean the same thing in both."""
    if region_ids:
        wanted = set(region_ids)
        return [r for r in mockup.regions if r.id in wanted]
    return list(mockup.regions)


def _atlas_id_for(db: Session, regions: list[MockupRegion]) -> int | None:
    """New assets a redraw pass creates (a text sprite lifted off a parent) file into
    whichever domain the FIRST already-built region in this run already lives in — same
    atlas a build run itself would have targeted."""
    if not regions:
        return None
    first_asset = db.get(Asset, regions[0].asset_id)
    return first_asset.atlas_id if first_asset else None


def _polish_regions(db: Session, mockup: Mockup, body: "PolishRegionsBody", emit) -> dict:
    """Standalone Polish step: a purely cosmetic AI redraw (upscale + clean edges) over a
    chosen set of already-built elements, or every built element if none are named.
    Decoupled from a specific build run so it can target one element, several, or
    everything on the mockup at any time. Does not touch lettering — that is the Text
    step's job (see `_apply_text_choices`), which normally runs before this."""
    project = get_project_or_404(db, mockup.project_id)
    to_polish = [r for r in _resolve_target_regions(mockup, body.region_ids) if r.asset_id and r.source != TEXT_SOURCE]

    if not to_polish:
        emit.emit("done", "done", "Nothing to polish — build elements first.")
        return {"polished": 0, "errors": []}

    atlas_id = _atlas_id_for(db, to_polish)
    errors: list[dict] = []
    summary: dict = {"processed": 0, "skipped": 0, "sprites": 0, "sprites_skipped": 0}
    try:
        summary = _redraw_built_regions(
            db, project, mockup, to_polish, atlas_id, body.provider,
            base_ops=["upscale", "clean_edges", "keep_colors"], apply_text_choice=False,
            raise_resolution=True, version_provider="llm:polish",
            running_label="Polishing", done_label="Polished", emit=emit,
            model=body.model, params=body.params, prompt_variant=body.prompt_variant,
            force=body.force,
        )
    except HTTPException as e:
        if is_quota_error(e.detail):
            emit.emit("polish", "error", f"Stopped — provider quota/rate limit reached: {e.detail}", data={"quota_exceeded": True})
            emit.stop()
        else:
            emit.emit("polish", "error", str(e.detail))
        errors.append({"region_id": None, "name": "polish", "error": e.detail})

    try:
        db.expire(mockup)
        prev_data = render_preview_screen(db, mockup)
        emit.emit("preview", "done", "Updated preview (polish)", data=prev_data)
    except Exception:
        pass

    if errors:
        emit.emit("done", "done", "Polish stopped after an error — see above")
    else:
        skipped = summary.get("skipped", 0)
        emit.emit("done", "done", (
            f"Polished {summary.get('processed', 0)} element"
            f"{'' if summary.get('processed') == 1 else 's'}"
            + (f" · kept {skipped} already polished (no provider call)" if skipped else "")
        ))
    return {
        "polished": summary.get("processed") if not errors else None,
        "skipped": summary.get("skipped", 0), "errors": errors,
    }


@router.post("/mockups/{mockup_id}/polish-regions/stream")
def polish_regions_stream(mockup_id: int, body: PolishRegionsBody):
    """SSE variant: cosmetic AI polish pass over an explicit set of already-built
    regions, or — if none are named — every built region. Backs the Polish step that
    runs after Build (and after Text, if used), for polishing one element at a time or
    all of them together."""
    with SessionLocal() as db:
        mockup = get_mockup_or_404(db, mockup_id)
        project_id = mockup.project_id

    def work(emit: Emitter):
        with SessionLocal() as db:
            mockup = get_mockup_or_404(db, mockup_id)
            return _polish_regions(db, mockup, body, emit)

    return sse_response(project_id, work, entity_type="mockup", entity_id=mockup_id)


def _apply_text_choices(db: Session, mockup: Mockup, body: "ApplyTextBody", emit) -> dict:
    """The Text step's actual work: an AI redraw pass that strips (or strips and
    isolates) the lettering on each named region, per its captions' own Remove/Extract
    choices — the choice `PATCH /labels/{id}` already saved onto each one. A region whose
    captions are all still on "Keep" is silently skipped even when swept up by an
    unfiltered "apply to all", so nothing costs a provider call unless it was actually
    asked for."""
    project = get_project_or_404(db, mockup.project_id)
    W, H = _mockup_size(mockup)
    to_apply = [
        r for r in _resolve_target_regions(mockup, body.region_ids)
        if r.asset_id and r.source != TEXT_SOURCE
        and any(l.text_mode in ("erase", "extract") for l, _box in _label_items_within(mockup, r, W, H))
    ]

    if not to_apply:
        emit.emit("done", "done", "Nothing to remove — mark elements Remove or Extract first.")
        return {"applied": 0, "errors": []}

    atlas_id = _atlas_id_for(db, to_apply)
    errors: list[dict] = []
    summary: dict = {"processed": 0, "skipped": 0, "sprites": 0, "sprites_skipped": 0}
    try:
        summary = _redraw_built_regions(
            db, project, mockup, to_apply, atlas_id, body.provider,
            base_ops=[], apply_text_choice=True, raise_resolution=False,
            version_provider="llm:text", running_label="Removing text on", done_label="Cleaned",
            emit=emit,
            model=body.model, params=body.params, prompt_variant=body.prompt_variant,
            force=body.force,
        )
    except HTTPException as e:
        if is_quota_error(e.detail):
            emit.emit("text", "error", f"Stopped — provider quota/rate limit reached: {e.detail}", data={"quota_exceeded": True})
            emit.stop()
        else:
            emit.emit("text", "error", str(e.detail))
        errors.append({"region_id": None, "name": "text", "error": e.detail})

    try:
        db.expire(mockup)
        prev_data = render_preview_screen(db, mockup)
        emit.emit("preview", "done", "Updated preview (text)", data=prev_data)
    except Exception:
        pass

    if errors:
        emit.emit("done", "done", "Stopped after an error — see above")
    else:
        skipped, sprites = summary.get("skipped", 0), summary.get("sprites", 0)
        emit.emit("done", "done", (
            f"Cleaned text off {summary.get('processed', 0)} element"
            f"{'' if summary.get('processed') == 1 else 's'}"
            + (f" · {sprites} text sprite{'' if sprites == 1 else 's'} extracted" if sprites else "")
            + (f" · kept {skipped} already cleaned (no provider call)" if skipped else "")
        ))
    return {
        "applied": summary.get("processed") if not errors else None,
        "skipped": summary.get("skipped", 0), "sprites": summary.get("sprites", 0),
        "errors": errors,
    }


@router.post("/mockups/{mockup_id}/apply-text/stream")
def apply_text_stream(mockup_id: int, body: ApplyTextBody):
    """SSE variant: runs the Text step's Remove/Extract choices against an explicit set
    of already-built regions, or — if none are named — every built region with a choice
    set. The step where those choices actually take effect, rather than sitting as
    inert flags until some later pass reads them."""
    with SessionLocal() as db:
        mockup = get_mockup_or_404(db, mockup_id)
        project_id = mockup.project_id

    def work(emit: Emitter):
        with SessionLocal() as db:
            mockup = get_mockup_or_404(db, mockup_id)
            return _apply_text_choices(db, mockup, body, emit)

    return sse_response(project_id, work, entity_type="mockup", entity_id=mockup_id)


@router.get("/mockups/{mockup_id}/step-status")
def step_status(mockup_id: int, db: Session = Depends(get_db)):
    """Element-by-element: what Text and Polish have actually produced for this screen, and
    what is still missing.

    Answers the question both steps otherwise leave the user guessing at — *which* of these
    nine elements already has its lettering off, which captions came out as their own
    sprites, which are still to do — which matters most exactly when it is hardest to tell:
    after a run that stopped halfway, where the screen is part done and nothing on it says
    where the line is.

    Deliberately computed from the same predicates the steps themselves resume on
    (`_redraw_is_current`, `_text_child_sprite`), so a badge saying "done" and the run
    deciding to skip it can never disagree — one of them being wrong would either hide
    missing work or make the UI look like it lost artwork that is still there.
    """
    mockup = get_mockup_or_404(db, mockup_id)
    W, H = _mockup_size(mockup)

    rows = []
    for region in mockup.regions:
        if region.source == TEXT_SOURCE or not region.asset_id:
            continue
        asset = db.get(Asset, region.asset_id)
        if not asset:
            continue
        version = asset.selected_version
        built = bool(
            version and version.processed_path and abs_path(version.processed_path).exists()
        )

        captions = []
        for label, _box in (_label_items_within(mockup, region, W, H) if W and H else []):
            mode = label.text_mode or "keep"
            sprite = _text_child_sprite(db, mockup, region, label) if mode == "extract" else None
            captions.append({
                "label_id": label.id,
                "text": label.text,
                "mode": mode,
                # Only "extract" captions produce a sprite of their own; an "erase" caption
                # is done when the parent is clean, and a "keep" caption asks for nothing.
                "sprite_ready": bool(sprite),
                "sprite_asset_id": sprite.id if sprite else None,
            })

        wants_text = any(c["mode"] in ("erase", "extract") for c in captions)
        cleaned = _redraw_is_current(asset, "llm:text")
        missing_sprites = [c["text"] for c in captions if c["mode"] == "extract" and not c["sprite_ready"]]
        rows.append({
            "region_id": region.id,
            "name": region.name,
            "asset_id": asset.id,
            "built": built,
            "polished": _redraw_is_current(asset, "llm:polish"),
            "text_needed": wants_text,
            "text_cleaned": cleaned,
            "text_done": bool(wants_text and cleaned and not missing_sprites),
            "captions": captions,
            "missing_sprites": missing_sprites,
        })

    text_todo = [r for r in rows if r["text_needed"] and not r["text_done"]]
    return {
        "regions": rows,
        "totals": {
            "elements": len(rows),
            "built": sum(1 for r in rows if r["built"]),
            "polished": sum(1 for r in rows if r["polished"]),
            "polish_missing": sum(1 for r in rows if not r["polished"]),
            "text_needed": sum(1 for r in rows if r["text_needed"]),
            "text_done": sum(1 for r in rows if r["text_done"]),
            "text_missing": len(text_todo),
            "sprites_expected": sum(
                1 for r in rows for c in r["captions"] if c["mode"] == "extract"
            ),
            "sprites_ready": sum(
                1 for r in rows for c in r["captions"] if c["mode"] == "extract" and c["sprite_ready"]
            ),
        },
    }


@router.post("/mockups/{mockup_id}/score")
def score_mockup_endpoint(mockup_id: int, db: Session = Depends(get_db)):
    """Fidelity report for a screen: every bound region scored against the pixels it was
    cut from, plus a whole-screen score of the rebuilt composite against the original
    screenshot. Persists each score onto the asset version that earned it."""
    from ..scoring import score_mockup

    mockup = get_mockup_or_404(db, mockup_id)
    return score_mockup(db, mockup)


@router.post("/regions/{region_id}/score")
def score_region_endpoint(region_id: int, db: Session = Depends(get_db)):
    from ..scoring import rescore_region

    region = get_region_or_404(db, region_id)
    mockup = get_mockup_or_404(db, region.mockup_id)
    result = rescore_region(db, mockup, region)
    if result is None:
        raise HTTPException(400, "Region has no bound asset with a usable image")
    return result


@router.post("/mockups/{mockup_id}/preview")
def preview_screen(mockup_id: int, db: Session = Depends(get_db)):
    """Composite every region's bound, generated asset into a screen-sized preview PNG,
    at the mockup's own image size — the same verification the CLI compositor does,
    surfaced directly in the tool."""
    mockup = get_mockup_or_404(db, mockup_id)
    return render_preview_screen(db, mockup)


def _asset_size(asset: Asset) -> tuple[int, int] | None:
    version = next(
        (v for v in asset.versions if v.id == asset.selected_version_id),
        asset.versions[-1] if asset.versions else None,
    )
    if version is None:
        return None
    p = abs_path(version.processed_path or version.raw_path)
    if not p.exists():
        return None
    with Image.open(p) as im:
        return im.size


@router.post("/mockups/{mockup_id}/export/screen")
def export_mockup_screen(mockup_id: int, name: str | None = None, db: Session = Depends(get_db)):
    """Reconstruct this mockup as a Unity screen: export every bound region/icon asset,
    then write Assets/Screens/<Screen>.screen.json describing where each sprite and text
    label sits, at the mockup's own reference resolution. ScreenLayoutBuilder.cs consumes
    this to build (and self-heal) an actual .prefab in the Unity project — the same
    placement math render_preview_screen already composites in-app, so the prefab always
    matches what the app shows as "the rebuilt screen"."""
    mockup = get_mockup_or_404(db, mockup_id)
    project = get_project_or_404(db, mockup.project_id)

    img_path = abs_path(mockup.image_path)
    if not img_path.exists():
        raise HTTPException(400, "Mockup image file missing")
    with Image.open(img_path) as src:
        W, H = src.size

    def _bound_asset(asset_id: int | None) -> Asset | None:
        asset = db.get(Asset, asset_id) if asset_id else None
        return asset if asset and asset.selected_version_id else None

    to_export: dict[int, Asset] = {}
    for region in mockup.regions:
        if (a := _bound_asset(region.asset_id)):
            to_export[a.id] = a
        if (a := _bound_asset(region.icon_asset_id)):
            to_export[a.id] = a

    export_errors = []
    for asset in to_export.values():
        try:
            export_asset(project, asset)
        except ExportError as e:
            export_errors.append({"asset_id": asset.id, "error": str(e)})
    try:
        export_atlases(project)
    except ExportError:
        pass
    failed_ids = {e["asset_id"] for e in export_errors}

    # Collect every placed element as {abs_rect, build(rect, z) -> element dict}, in the
    # order it should ultimately draw (backgrounds biggest-first so nested containers are
    # available before anything that will nest inside them; labels last so they're always
    # on top). Rects here are ABSOLUTE (normalized to the whole screen) — used to work out
    # containment below, then converted to be relative to whichever parent each element
    # gets assigned before actually building the element dicts.
    items: list[dict] = []
    missing = []
    for region in sorted(mockup.regions, key=lambda r: -(r.w * r.h)):
        bg_asset = _bound_asset(region.asset_id)
        if bg_asset is None or bg_asset.id in failed_ids:
            missing.append(region.name)
            continue
        box = {"x": region.x / 100, "y": region.y / 100, "w": region.w / 100, "h": region.h / 100}
        fit_mode = fit_mode_for(bg_asset.type, bg_asset.nine_slice)
        items.append({
            "kind": "sprite",
            "abs_rect": box,
            "build": lambda rect, z, asset=bg_asset, fit=fit_mode, mirror=region.mirror:
                screen_element(project, asset, rect, z, fit=fit, mirror=mirror),
        })

        icon_asset = _bound_asset(region.icon_asset_id)
        if icon_asset is not None and icon_asset.id not in failed_ids:
            icon_size = _asset_size(icon_asset)
            box_h_px = box["h"] * H
            ih = 0.78 * box_h_px
            iw = ih * icon_size[0] / icon_size[1] if icon_size and icon_size[1] else ih
            icon_box = {
                "x": box["x"] + (0.12 * box_h_px) / W,
                "y": box["y"] + ((box_h_px - ih) / 2) / H,
                "w": iw / W,
                "h": ih / H,
            }
            items.append({
                "kind": "sprite",
                "abs_rect": icon_box,
                "build": lambda rect, z, asset=icon_asset, mirror=region.mirror:
                    screen_element(project, asset, rect, z, fit="contain", mirror=mirror),
            })

    # Nest each element under the smallest OTHER element that (almost) fully contains it —
    # e.g. a button caption nests under its button, nav icons/labels nest under the nav
    # bar — instead of every sprite/label sitting as a flat sibling under the screen root.
    # Purely geometric (same containment_ratio used for background/foreground detection
    # elsewhere), since there's no explicit parent/child link in the data model. Only
    # sprites can be a parent — a label is text, never a meaningful container, and a
    # caption's box can easily overlap a neighbouring icon widely enough to "contain" it
    # by area alone, which would otherwise nest a sprite inside a Text object.
    CONTAIN = 0.75
    areas = [it["abs_rect"]["w"] * it["abs_rect"]["h"] for it in items]
    parent_of: list[int | None] = [None] * len(items)
    for i, it in enumerate(items):
        inner = (it["abs_rect"]["x"], it["abs_rect"]["y"], it["abs_rect"]["w"], it["abs_rect"]["h"])
        best_j, best_area = None, None
        for j, jt in enumerate(items):
            if j == i or jt["kind"] != "sprite" or areas[j] <= areas[i]:
                continue
            outer = (jt["abs_rect"]["x"], jt["abs_rect"]["y"], jt["abs_rect"]["w"], jt["abs_rect"]["h"])
            if containment_ratio(inner, outer) >= CONTAIN and (best_area is None or areas[j] < best_area):
                best_j, best_area = j, areas[j]
        parent_of[i] = best_j

    elements = []
    for i, it in enumerate(items):
        ar = it["abs_rect"]
        parent = parent_of[i]
        if parent is None:
            rect = ar
        else:
            par = items[parent]["abs_rect"]
            rect = {
                "x": (ar["x"] - par["x"]) / par["w"] if par["w"] else ar["x"],
                "y": (ar["y"] - par["y"]) / par["h"] if par["h"] else ar["y"],
                "w": ar["w"] / par["w"] if par["w"] else ar["w"],
                "h": ar["h"] / par["h"] if par["h"] else ar["h"],
            }
        el = it["build"](rect, i)
        el["parent"] = parent if parent is not None else -1
        elements.append(el)

    screen_name = (name or mockup.name or f"Mockup{mockup.id}").strip() or f"Mockup{mockup.id}"
    try:
        result = export_screen_layout(project, screen_name, elements, reference=f"{W}x{H}")
        reference_path = export_screen_reference(project, screen_name, img_path)
    except ExportError as e:
        raise HTTPException(400, str(e))
    return {**result, "reference": reference_path, "missing": missing, "export_errors": export_errors}
