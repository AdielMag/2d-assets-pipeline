from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import AssetVersion
from ..processing.nine_slice import detect_borders
from ..processing.transparency import has_real_alpha, remove_background
from ..processing.trim import (
    MAX_RESOLUTION_DIM,
    fit_to_resolution,
    parse_resolution,
    subject_aspect_mismatch,
    trim_for_fit,
)
from ..processing.upres import upres_to
from ..progress import Emitter, estimate_tokens, get_stored_runs, is_quota_error, make_emitter, sse_response
from ..processing.reference import letterbox_reference, snap_ratio
from ..prompting import (
    REFERENCE_OP_GROUPS,
    REFERENCE_OPS,
    compose_prompt,
    compose_sections,
    external_prompt,
    extraction_op_keys,
    grouped_op_keys,
)
from ..providers import ProviderError, get_enabled_provider, get_provider, resolve_model
from ..providers.registry import (
    extraction_model_warning,
    model_aspect_options,
    resolve_extraction_model,
    resolve_params,
)
from ..schemas import AssetOut
from ..storage import abs_path, new_asset_path, new_work_path, rel_path
from .assets import get_asset_or_404
from .projects import get_project_or_404

router = APIRouter(prefix="/api", tags=["generate"])


class GenerateBody(BaseModel):
    provider: str  # antigravity | higgsfield
    model: str | None = None
    prompt: str | None = None  # updated user prompt; falls back to asset.prompt
    prompt_mode: str | None = None  # "generate" | "reference"
    reference_ops: list[str] | None = None
    visual_model: str | None = None  # visual/image generation model name to inject in prompt (antigravity only)
    reference_paths: list[str] = []  # storage-relative reference images (e.g. mockup crop)
    override_entire_prompt: bool | None = None  # override for 1 generation or save on asset


@router.get("/reference-ops")
def reference_ops_catalogue():
    """The tick-able reference operations, so the UI renders the same list the server
    composes from and the two can't drift.

    `ops` are standalone chips; `groups` are one toggle with a picker over several ops
    (see prompting.REFERENCE_OP_GROUPS). A grouped key appears only under its group, so
    the client stays generic — it renders whatever grouping the server declares instead
    of hard-coding which keys belong together."""
    grouped = grouped_op_keys()
    return {
        "ops": [{"key": k, "label": lbl, "instruction": txt}
                for k, (lbl, txt) in REFERENCE_OPS.items() if k not in grouped],
        "groups": [
            {**g, "choices": [{**c, "instruction": REFERENCE_OPS[c["key"]][1]}
                              for c in g["choices"]]}
            for g in REFERENCE_OP_GROUPS
        ],
        # Which ticked ops make this an extraction ("keep only X") rather than a normal
        # edit — the client uses it to warn when the selected model isn't the one
        # measured to work for that job. See prompting.extraction_op_keys.
        "extraction_keys": sorted(extraction_op_keys()),
    }


@router.get("/assets/{asset_id}/composed-prompt")
def composed_prompt(
    asset_id: int,
    override_entire_prompt: bool | None = None,
    prompt_mode: str | None = None,
    reference_ops: str | None = None,  # comma-separated keys
    db: Session = Depends(get_db)
):
    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    is_override = override_entire_prompt if override_entire_prompt is not None else asset.override_entire_prompt
    # mode/ops may be supplied un-saved so the preview updates the instant a toggle is
    # clicked, rather than only after a round-trip through PATCH.
    mode = prompt_mode or asset.prompt_mode or "generate"
    ops = (
        [o for o in reference_ops.split(",") if o]
        if reference_ops is not None
        else list(asset.reference_ops or [])
    )
    # compose_prompt only reads attributes, so a plain stand-in is enough — and unlike a
    # detached ORM instance it can't be accidentally flushed back to the DB.
    preview = SimpleNamespace(
        type=asset.type, prompt=asset.prompt, aspect_ratio=asset.aspect_ratio,
        resolution=asset.resolution, nine_slice=asset.nine_slice,
        prompt_mode=mode, reference_ops=ops, override_entire_prompt=is_override,
    )
    full_prompt = compose_prompt(project, preview, override_entire_prompt=is_override)
    return {
        "sections": compose_sections(
            project, asset.type, asset.prompt, asset.aspect_ratio, asset.resolution,
            is_sliced=(asset.nine_slice is not None),
            override_entire_prompt=is_override, prompt_mode=mode, reference_ops=ops,
        ),
        "full": full_prompt,
        "external": external_prompt(project, preview, override_entire_prompt=is_override),
        # Input-side only — the same formula `estimate_tokens` uses for the real
        # post-generation count (see progress.py), so what's shown before Generate and
        # what RunProgress shows after don't disagree. Output/thinking tokens are
        # unknown until the model actually responds, so this is a floor, not a total.
        "estimated_tokens": estimate_tokens(
            prompt=full_prompt, image_count=len(asset.reference_images or []),
        ),
    }


@router.get("/assets/{asset_id}/estimate-cost")
def estimate_generation_cost(
    asset_id: int,
    provider: str,
    model: str | None = None,
    override_entire_prompt: bool | None = None,
    prompt_mode: str | None = None,
    reference_ops: str | None = None,
    db: Session = Depends(get_db),
):
    """Real per-request credit cost, straight from Higgsfield's own `generate cost`
    (no job created) — meaningful only for a per-image-billed provider. Antigravity is
    a flat subscription with nothing to estimate, so it always reports unsupported
    rather than a made-up number."""
    if provider != "higgsfield":
        return {"supported": False, "credits": None}

    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    is_override = override_entire_prompt if override_entire_prompt is not None else asset.override_entire_prompt
    mode = prompt_mode or asset.prompt_mode or "generate"
    ops = (
        [o for o in reference_ops.split(",") if o]
        if reference_ops is not None
        else list(asset.reference_ops or [])
    )
    preview = SimpleNamespace(
        type=asset.type, prompt=asset.prompt, aspect_ratio=asset.aspect_ratio,
        resolution=asset.resolution, nine_slice=asset.nine_slice,
        prompt_mode=mode, reference_ops=ops, override_entire_prompt=is_override,
    )
    full_prompt = compose_prompt(project, preview, override_entire_prompt=is_override)
    ref_paths = [abs_path(p) for p in (asset.reference_images or [])]

    hf = get_provider("higgsfield")
    # The same params the real call will send, or the quote is for a different job:
    # gpt_image_2 is 0.75 credits at `--quality low` and 7 at its `high` default.
    resolved_model = resolve_model("higgsfield", model)
    credits = hf.estimate_cost(
        full_prompt, model=resolved_model, reference_images=ref_paths,
        params=resolve_params("higgsfield", resolved_model),
    )
    return {"supported": True, "credits": credits}


@router.get("/assets/{asset_id}/generations")
def get_asset_generations(asset_id: int, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    return get_stored_runs(asset.project_id, entity_id=asset.id, entity_type="asset")


def _generate_version(db: Session, asset, project, body: GenerateBody, emit):
    """Shared core for the plain and streaming generate paths. `emit` is an Emitter (or
    _NoEmit); it reports each pipeline stage and drops an intermediate thumbnail so the
    UI can show raw -> keyed -> trimmed -> final."""
    if body.prompt is not None:
        asset.prompt = body.prompt
    if body.override_entire_prompt is not None:
        asset.override_entire_prompt = body.override_entire_prompt
    if body.prompt_mode is not None:
        asset.prompt_mode = body.prompt_mode
    if body.reference_ops is not None:
        asset.reference_ops = body.reference_ops
    if any(v is not None for v in (
        body.prompt, body.override_entire_prompt, body.prompt_mode, body.reference_ops
    )):
        db.commit()

    is_override = body.override_entire_prompt if body.override_entire_prompt is not None else asset.override_entire_prompt

    prompt = compose_prompt(project, asset, override_entire_prompt=is_override)
    sections = compose_sections(
        project, asset.type, asset.prompt, asset.aspect_ratio, asset.resolution,
        is_sliced=(asset.nine_slice is not None), override_entire_prompt=is_override,
        prompt_mode=asset.prompt_mode or "generate", reference_ops=list(asset.reference_ops or []),
    )
    provider = get_enabled_provider(body.provider)  # ProviderError -> caller maps to HTTP
    raw = new_asset_path(db, asset, f"{asset.name}-raw")

    ref_paths = body.reference_paths if body.reference_paths else list(asset.reference_images or [])
    refs = [abs_path(p) for p in ref_paths]
    for ref in refs:
        if not ref.exists():
            raise ProviderError(f"Reference image not found: {ref.name}")

    # A "keep only X" generation is prepared differently from the rest of reference mode:
    # it gets the model that can actually do it, and its references are letterboxed onto
    # magenta so the composition fits the model's canvas and the prompt's "background is
    # already magenta" premise is true. See providers.registry.EXTRACTION_MODEL and
    # processing.reference for the measurements behind both.
    extracting = bool(extraction_op_keys() & set(sections.get("reference_ops") or []))
    if extracting:
        model = resolve_extraction_model(body.provider, body.model)
    else:
        model = resolve_model(body.provider, body.model)
    params = resolve_params(body.provider, model, getattr(body, "params", None))

    if extracting:
        warning = extraction_model_warning(body.provider, model)
        if warning:
            emit.emit("model", "done", f"⚠ {warning}", data={"details": warning})

    if extracting and refs:
        allowed = model_aspect_options(body.provider, model)
        prepared = []
        for i, ref in enumerate(refs):
            with Image.open(ref) as im:
                ratio = snap_ratio(im.width, im.height, allowed)
            dest = new_work_path(asset.project_id, f"{asset.name}-extract-ref-{i}")
            prepared.append(letterbox_reference(ref, dest, ratio))
        refs = prepared

    toks = estimate_tokens(prompt, output="", image_count=len(refs))

    gen_data = {
        "prompt": prompt,
        "user_prompt": asset.prompt,
        "sections": sections,
        "provider": provider.name,
        "model": model,
        "visual_model": getattr(body, 'visual_model', None),
        # Recorded because params change both the output and the price, so a run log
        # without them can't explain why two runs of the "same" model differ.
        "params": params or None,
        "reference_images": [r.name for r in refs],
        "tokens": toks,
    }

    emit.emit("generate", "running", f"Generating raw image · {provider.name}/{model or 'default'}…", data=gen_data)
    provider.generate(
        prompt, raw, reference_images=refs or None, transparent=True, model=model,
        visual_model=getattr(body, 'visual_model', None),
        reference_mode=(sections["prompt_mode"] == "reference"),
        params=params,
    )

    cmd_str = getattr(provider, "last_command", None)
    if cmd_str:
        gen_data["command"] = cmd_str

    gen_data_done = {
        **gen_data,
        "tokens": estimate_tokens(prompt, output="Raw image generated (1024x1024 PNG)", image_count=len(refs)),
    }
    with Image.open(raw) as img:
        emit.emit("generate", "done", "Raw image generated", image=emit.preview(img, "raw"), data=gen_data_done)

    processed = new_asset_path(db, asset, asset.name)
    with Image.open(raw) as img:
        if provider.needs_transparency_postprocess:
            bg_data = {"details": "Chroma-key magenta background removal (#FF00FF)", "tokens": estimate_tokens("Background removal")}
            emit.emit("background", "running", "Removing background (magenta key)…", data=bg_data)
            out = remove_background(img)
            emit.emit("background", "done", "Background removed", image=emit.preview(out, "keyed"), data=bg_data)
        else:
            out = img.convert("RGBA")
        sliced = asset.nine_slice is not None
        skew = None if sliced else subject_aspect_mismatch(out, asset.aspect_ratio)
        out = trim_for_fit(out, sliced, asset.aspect_ratio)
        trim_msg = "Trimmed tight to fill rect" if sliced else "Trimmed & centered"
        details = f"Alpha trim mode: {trim_msg}"
        if skew:
            actual, _ = skew
            trim_msg += f" ⚠ generated subject is {actual:.2f}:1, not {asset.aspect_ratio}"
            details += (
                f" — WARNING: the provider returned art shaped {actual:.2f}:1 instead of the "
                f"requested {asset.aspect_ratio}, so it looks stretched/squashed. Padding kept "
                f"it undistorted, but regenerate for correctly proportioned art."
            )
        trim_data = {"details": details, "tokens": estimate_tokens(trim_msg)}
        emit.emit("trim", "done", trim_msg, image=emit.preview(out, "trimmed"), data=trim_data)

        out = fit_to_resolution(out, asset.resolution)
        fit_msg = f"Fit to {asset.resolution}"
        fit_data = {"details": f"Resized & centered into target resolution box {asset.resolution}", "tokens": estimate_tokens(fit_msg)}
        emit.emit("fit", "done", fit_msg, image=emit.preview(out, "final"), data=fit_data)
        out.save(processed)

    version = AssetVersion(
        asset_id=asset.id,
        provider=provider.name,
        model=model,
        composed_prompt=prompt,
        reference_paths=ref_paths,
        raw_path=rel_path(raw),
        processed_path=rel_path(processed),
    )
    db.add(version)
    db.commit()
    asset.selected_version_id = version.id
    # Give UI elements a sensible default 9-slice so they're export-ready out of the
    # box; the user can still fine-tune the guides.
    if asset.nine_slice is not None:
        with Image.open(abs_path(version.processed_path)) as img:
            asset.nine_slice = detect_borders(img)
    db.commit()
    db.refresh(asset)
    return asset


@router.post("/assets/{asset_id}/generate", response_model=AssetOut)
def generate(asset_id: int, body: GenerateBody, db: Session = Depends(get_db)):
    """Plain (non-streaming) generate path — also used by callers that hit the REST API
    directly (scripts, external agents) rather than the browser's `/stream` UI, so it
    still logs a real run (see make_emitter) rather than silently doing nothing."""
    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    emit = make_emitter(project.id, entity_type="asset", entity_id=asset.id)
    try:
        result = _generate_version(db, asset, project, body, emit)
        emit.emit("__done__", "done", "", data={"asset_id": asset.id})
        return result
    except ProviderError as e:
        msg = str(e)
        emit.emit("__error__", "error", msg, data={"quota_exceeded": True} if is_quota_error(msg) else None)
        if "disabled" in msg or "Unknown" in msg:
            status = 403           # provider off in settings / not a real provider
        elif "not found" in msg:
            status = 400           # bad reference path
        else:
            status = 502           # provider/generation failure
        raise HTTPException(status, msg)


@router.post("/assets/{asset_id}/generate/stream")
def generate_stream(asset_id: int, body: GenerateBody):
    """SSE variant of generate: same work, emitting live step events + intermediate
    thumbnails. Runs in a worker thread with its own DB session."""
    # validate up front (on the request thread) so obvious errors are plain HTTP, not SSE
    with SessionLocal() as db:
        asset = get_asset_or_404(db, asset_id)
        project_id = asset.project_id
        get_project_or_404(db, project_id)

    def work(emit: Emitter):
        with SessionLocal() as db:
            asset = get_asset_or_404(db, asset_id)
            project = get_project_or_404(db, asset.project_id)
            _generate_version(db, asset, project, body, emit)
            return {"asset_id": asset_id}

    return sse_response(project_id, work, entity_type="asset", entity_id=asset_id)



@router.post("/assets/{asset_id}/upload-version", response_model=AssetOut)
async def upload_version(
    asset_id: int, file: UploadFile, preserve_framing: bool = Form(False), db: Session = Depends(get_db),
):
    """Add a version from an image generated by hand in an external LLM/chat UI using
    the copy-pasted composed prompt (see /assets/{id}/composed-prompt's "external"
    field). Run through the same magenta-key removal as an in-tool Gemini generation,
    unless the upload already has real alpha (e.g. a provider with native transparency).

    `preserve_framing` skips the trim/re-center pass: it's for edits of an already-framed
    image (the erase brush) rather than a fresh raw generation, where re-trimming to the
    post-edit alpha bbox would rescale/recenter the art and silently change its apparent
    size — the whole point of an edit is to change only the painted pixels."""
    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    ext = (file.filename or "upload.png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, "Unsupported image type")

    raw = new_asset_path(db, asset, f"{asset.name}-raw", ext)
    raw.write_bytes(await file.read())

    processed = new_asset_path(db, asset, asset.name)
    with Image.open(raw) as img:
        if preserve_framing:
            # Editor upload (erase/clone tool): the alpha channel is already exactly what
            # the user painted, including a fully-opaque result if they cloned over every
            # transparent pixel. Never run chroma-key/rembg background removal here — with
            # no magenta key present it falls through to rembg, which guesses a subject
            # silhouette from the flat art and can carve transparency back into regions the
            # user deliberately made opaque, silently reverting their edit on save.
            out = img.convert("RGBA")
        else:
            out = img.convert("RGBA") if has_real_alpha(img) else remove_background(img)
            out = trim_for_fit(out, asset.nine_slice is not None, asset.aspect_ratio)
        out = fit_to_resolution(out, asset.resolution)
        out.save(processed)

    version = AssetVersion(
        asset_id=asset.id,
        provider="manual",
        composed_prompt=external_prompt(project, asset),
        raw_path=rel_path(raw),
        processed_path=rel_path(processed),
    )
    db.add(version)
    db.commit()
    asset.selected_version_id = version.id
    if asset.nine_slice is not None:
        with Image.open(abs_path(version.processed_path)) as img:
            asset.nine_slice = detect_borders(img)
    db.commit()
    db.refresh(asset)
    return asset


@router.post("/assets/{asset_id}/upscale", response_model=AssetOut)
def upscale(asset_id: int, db: Session = Depends(get_db)):
    """Double the asset's target resolution (capped) and re-derive the processed image
    from its still-pristine raw source — not a blur-up of the small processed copy.

    Where the raw is already bigger than the new box this is just a re-fit, as sharp as
    the raw allows. Where it isn't — always the case for an extraction, which can only
    ever hold as much detail as the screenshot it was cut from — Real-ESRGAN synthesises
    the missing detail instead of Lanczos smearing what is there. That is why enlargement
    lives here and not in `fit_to_resolution`, which no longer upscales at all: an
    extracted sprite is stored at its native size and grown only when something actually
    asks for a bigger one."""
    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    version = next((v for v in asset.versions if v.id == asset.selected_version_id), None)
    if version is None or not version.raw_path:
        raise HTTPException(400, "No generated version to upscale")
    raw = abs_path(version.raw_path)
    if not raw.exists():
        raise HTTPException(400, "Raw image file missing")

    w, h = parse_resolution(asset.resolution or "256x256")
    new_resolution = f"{min(w * 2, MAX_RESOLUTION_DIM)}x{min(h * 2, MAX_RESOLUTION_DIM)}"

    # Doubling that hits the cap on both axes leaves the target box exactly where it was,
    # and re-deriving into an unchanged box is not harmless: an asset whose current image
    # is *larger* than the box (an import or upload keeps its own pixel size) gets fitted
    # down into it, so "Upscale" hands back a smaller image than it started with. Refuse
    # instead — there is no bigger version to produce.
    if new_resolution == asset.resolution:
        raise HTTPException(
            400,
            f"Already at the {MAX_RESOLUTION_DIM}px limit — doubling would be capped back to "
            f"{new_resolution}, and re-deriving would shrink this image to fit that box.",
        )

    target_w, target_h = parse_resolution(new_resolution)
    processed = new_asset_path(db, asset, asset.name)
    with Image.open(raw) as img:
        out = img.convert("RGBA") if has_real_alpha(img) else remove_background(img)
        # An extraction is already aligned to its region rect; trimming it re-centres the
        # art inside a fresh margin and it no longer lands where it was cut from.
        if asset.source != "extract":
            out = trim_for_fit(out, asset.nine_slice is not None, asset.aspect_ratio)
        out = fit_to_resolution(out, new_resolution)  # shrink only; a no-op when growing
        out, method = upres_to(out, target_w, target_h)
        out.save(processed)

    new_version = AssetVersion(
        asset_id=asset.id,
        provider=f"{version.provider}+upscale:{method}",
        model=version.model,
        composed_prompt=version.composed_prompt,
        raw_path=version.raw_path,
        processed_path=rel_path(processed),
    )
    db.add(new_version)
    asset.resolution = new_resolution
    db.commit()
    asset.selected_version_id = new_version.id
    if asset.nine_slice is not None:
        with Image.open(abs_path(new_version.processed_path)) as img:
            asset.nine_slice = detect_borders(img)
    db.commit()
    db.refresh(asset)
    return asset


@router.post("/assets/{asset_id}/downscale", response_model=AssetOut)
def downscale(asset_id: int, db: Session = Depends(get_db)):
    """Halve the asset's target resolution (minimum 16x16) and re-derive the processed
    image from its still-pristine raw generation."""
    asset = get_asset_or_404(db, asset_id)
    project = get_project_or_404(db, asset.project_id)
    version = next((v for v in asset.versions if v.id == asset.selected_version_id), None)
    if version is None or not version.raw_path:
        raise HTTPException(400, "No generated version to downscale")
    raw = abs_path(version.raw_path)
    if not raw.exists():
        raise HTTPException(400, "Raw image file missing")

    w, h = parse_resolution(asset.resolution or "256x256")
    new_resolution = f"{max(16, round(w / 2))}x{max(16, round(h / 2))}"

    processed = new_asset_path(db, asset, asset.name)
    with Image.open(raw) as img:
        out = img.convert("RGBA") if has_real_alpha(img) else remove_background(img)
        out = trim_for_fit(out, asset.nine_slice is not None, asset.aspect_ratio)
        out = fit_to_resolution(out, new_resolution)
        out.save(processed)

    new_version = AssetVersion(
        asset_id=asset.id,
        provider=f"{version.provider}+downscale",
        model=version.model,
        composed_prompt=version.composed_prompt,
        raw_path=version.raw_path,
        processed_path=rel_path(processed),
    )
    db.add(new_version)
    asset.resolution = new_resolution
    db.commit()
    asset.selected_version_id = new_version.id
    if asset.nine_slice is not None:
        with Image.open(abs_path(new_version.processed_path)) as img:
            asset.nine_slice = detect_borders(img)
    db.commit()
    db.refresh(asset)
    return asset
