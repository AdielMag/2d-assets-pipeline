import io
import platform
import re
import subprocess
from math import gcd
from pathlib import PurePath

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import layout
from ..db import get_db
from ..models import Asset, AssetVersion, Atlas, MockupRegion
from ..processing.nine_slice import detect_borders
from ..processing.transparency import has_real_alpha, remove_flat_background
from ..processing.trim import fit_to_resolution, trim_for_fit
from ..schemas import (
    ASSET_TYPES,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    AssetCreate,
    AssetOut,
    AssetUpdate,
)
from ..storage import abs_path, new_asset_path, rel_path
from .projects import get_project_or_404

router = APIRouter(prefix="/api", tags=["assets"])


def get_asset_or_404(db: Session, asset_id: int) -> Asset:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    return asset


@router.get("/projects/{project_id}/assets", response_model=list[AssetOut])
def list_assets(project_id: int, db: Session = Depends(get_db)):
    get_project_or_404(db, project_id)
    return db.scalars(
        select(Asset).where(Asset.project_id == project_id).order_by(Asset.updated_at.desc())
    ).all()


@router.post("/projects/{project_id}/assets", response_model=AssetOut)
def create_asset(project_id: int, body: AssetCreate, db: Session = Depends(get_db)):
    get_project_or_404(db, project_id)
    if body.type not in ASSET_TYPES:
        raise HTTPException(400, f"type must be one of {ASSET_TYPES}")
    is_sliced = body.is_sliced if body.is_sliced is not None else (body.type == "ui_element")
    initial_ns = {"l": 32, "t": 32, "r": 32, "b": 32} if is_sliced else None
    asset = Asset(
        project_id=project_id, name=body.name, type=body.type, prompt=body.prompt,
        atlas_id=body.atlas_id,
        aspect_ratio=body.aspect_ratio or DEFAULT_ASPECT_RATIO,
        resolution=body.resolution or DEFAULT_RESOLUTION,
        nine_slice=initial_ns,
        reference_images=[],
        override_entire_prompt=body.override_entire_prompt,
        prompt_mode=body.prompt_mode,
        reference_ops=body.reference_ops,
    )
    db.add(asset)
    db.commit()
    return asset


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: int, db: Session = Depends(get_db)):
    return get_asset_or_404(db, asset_id)


@router.patch("/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, body: AssetUpdate, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    updates = body.model_dump(exclude_unset=True)
    if "type" in updates and updates["type"] not in ASSET_TYPES:
        raise HTTPException(400, f"type must be one of {ASSET_TYPES}")
    relocating = any(
        field in updates and updates[field] != getattr(asset, field)
        for field in ("atlas_id", "name")
    )
    for field, value in updates.items():
        setattr(asset, field, value)
    db.commit()
    if relocating:
        # The asset's folder is its name inside its domain's path, so either change moves
        # it. `reconcile` does the move and rewrites the paths that pointed at the files.
        layout.reconcile(db, asset.project_id)
        db.refresh(asset)
    return asset


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    # Resolved before the row goes: the folder is derived from the asset's name and its
    # domain chain, neither of which is reachable once the asset is deleted.
    folder = layout.asset_dir(db, asset)
    db.execute(
        update(MockupRegion)
        .where(MockupRegion.asset_id == asset_id)
        .values(asset_id=None)
    )
    db.execute(
        update(MockupRegion)
        .where(MockupRegion.icon_asset_id == asset_id)
        .values(icon_asset_id=None)
    )
    db.delete(asset)
    db.commit()
    layout.remove_dir(folder)
    return {"ok": True}


def get_version_or_404(db: Session, asset_id: int, version_id: int) -> AssetVersion:
    version = db.get(AssetVersion, version_id)
    if not version or version.asset_id != asset_id:
        raise HTTPException(404, "Version not found")
    return version


@router.delete("/assets/{asset_id}/versions/{version_id}", response_model=AssetOut)
def delete_asset_version(asset_id: int, version_id: int, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    version = get_version_or_404(db, asset_id, version_id)
    if len(asset.versions) <= 1:
        raise HTTPException(400, "Can't delete an asset's only version")

    for rel in (version.raw_path, version.processed_path):
        if not rel:
            continue
        path = abs_path(rel)
        if path.exists():
            path.unlink()

    was_selected = asset.selected_version_id == version.id
    db.delete(version)
    db.commit()
    db.refresh(asset)
    if was_selected:
        remaining = sorted(asset.versions, key=lambda v: v.created_at)
        asset.selected_version_id = remaining[-1].id if remaining else None
        db.commit()
        db.refresh(asset)
    return asset


@router.post("/assets/{asset_id}/versions/{version_id}/reveal")
def reveal_asset_version(asset_id: int, version_id: int, db: Session = Depends(get_db)):
    """Open the OS file browser with this version's image pre-selected. Local-only:
    this server and its client always run on the same machine (see CLAUDE.md), so
    shelling out to the desktop's own file manager carries no remote-exposure risk."""
    get_asset_or_404(db, asset_id)
    version = get_version_or_404(db, asset_id, version_id)
    rel = version.processed_path or version.raw_path
    if not rel:
        raise HTTPException(404, "This version has no file on disk")
    path = abs_path(rel)
    if not path.exists():
        raise HTTPException(404, "File no longer exists on disk")

    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(["explorer", "/select,", str(path)])
        elif system == "Darwin":
            subprocess.run(["open", "-R", str(path)])
        else:
            subprocess.run(["xdg-open", str(path.parent)])
    except OSError as e:
        raise HTTPException(500, f"Failed to open file browser: {e}")
    return {"ok": True}


@router.post("/assets/{asset_id}/references", response_model=AssetOut)
async def upload_asset_reference(
    asset_id: int, file: UploadFile, db: Session = Depends(get_db)
):
    asset = get_asset_or_404(db, asset_id)
    ext = (file.filename or "ref.png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, "Unsupported image type")
    dest = new_asset_path(db, asset, f"{asset.name}-ref", ext)
    dest.write_bytes(await file.read())
    current_refs = list(asset.reference_images or [])
    current_refs.append(rel_path(dest))
    asset.reference_images = current_refs
    db.commit()
    db.refresh(asset)
    return asset


@router.delete("/assets/{asset_id}/references", response_model=AssetOut)
def remove_asset_reference(asset_id: int, path: str, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    current_refs = list(asset.reference_images or [])
    asset.reference_images = [p for p in current_refs if p != path]
    db.commit()
    db.refresh(asset)
    # Only delete the file if nothing else in the project still points at it — a
    # reference is very often an earlier version's own PNG, which the version still owns.
    if path not in layout.owners(db, asset.project_id):
        layout.remove_file(path)
    return asset


IMPORT_EXTS = ("png", "jpg", "jpeg", "webp", "bmp")


def _optional_int(value: str | None) -> int | None:
    """Parse an optional multipart field. Browsers send '' for a cleared <select>, which
    FastAPI would reject as a bad int — treat blank as "not set"."""
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise HTTPException(400, f"Expected a number, got {value!r}")


def _aspect_ratio_of(img: Image.Image) -> str:
    """The image's own shape as a reduced "W:H" string, so an imported asset declares the
    aspect it actually has rather than inheriting the 1:1 generation default."""
    divisor = gcd(img.width, img.height) or 1
    return f"{img.width // divisor}:{img.height // divisor}"


def _decode_upload(data: bytes) -> Image.Image:
    """Decode uploaded bytes, turning a corrupt/mislabelled image into a 400 rather than a
    500 — the extension check only proves what the file claims to be. Decoded before
    anything is written to storage, so a rejected drop leaves no orphan file behind."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Could not read that image file")


@router.post("/projects/{project_id}/assets/import-cutout", response_model=AssetOut)
async def import_cutout(
    project_id: int,
    file: UploadFile,
    name: str = Form(""),
    type: str = Form("ui_element"),
    atlas_id: str | None = Form(None),
    resolution: str | None = Form(None),
    is_sliced: bool | None = Form(None),
    trim: bool = Form(True),
    db: Session = Depends(get_db),
):
    """Import a hand-made image whose background is the magenta key (#FF00FF), key it out
    and save it as a new asset in a domain — the same chroma-key + trim + fit pass an
    in-tool Gemini generation goes through (see generate._generate_version), minus the
    generation. An upload that already carries real alpha is passed through untouched, so
    re-importing an exported PNG doesn't run it through the keyer a second time.

    Blank `resolution` keeps the image at its own size: unlike a generation, an import has
    no target box to honour, and `fit_to_resolution` would otherwise shrink it into the
    256x256 default."""
    get_project_or_404(db, project_id)
    if type not in ASSET_TYPES:
        raise HTTPException(400, f"type must be one of {ASSET_TYPES}")

    parsed_atlas_id = _optional_int(atlas_id)
    if parsed_atlas_id is not None:
        atlas = db.get(Atlas, parsed_atlas_id)
        if not atlas or atlas.project_id != project_id:
            raise HTTPException(400, "Domain not found in this project")

    filename = file.filename or "import.png"
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in IMPORT_EXTS:
        raise HTTPException(400, f"Unsupported image type '{ext}' — use {', '.join(IMPORT_EXTS)}")

    asset_name = (name or PurePath(filename).stem).strip()
    if not asset_name:
        raise HTTPException(400, "Asset name is required")

    # `parse_resolution` silently falls back to 256x256 on anything it can't read, which
    # would fit the image to one box while the DB recorded the typo as its size.
    target_resolution = (resolution or "").strip()
    if target_resolution and not re.fullmatch(r"\d+\s*x\s*\d+", target_resolution, re.IGNORECASE):
        raise HTTPException(400, f"Resolution must look like 256x256, got {target_resolution!r}")

    data = await file.read()
    sliced = is_sliced if is_sliced is not None else (type == "ui_element")
    with _decode_upload(data) as img:
        out = img.convert("RGBA") if has_real_alpha(img) else remove_flat_background(img)
        if trim:
            out = trim_for_fit(out, sliced)
        out = fit_to_resolution(out, target_resolution or None)
        # A blank resolution box means "keep what came in", so record the size the file
        # actually has — a declared box the PNG doesn't match is what _backfill_resolutions
        # exists to repair.
        final_resolution = target_resolution or f"{out.width}x{out.height}"
        aspect_ratio = _aspect_ratio_of(out)

    asset = Asset(
        project_id=project_id,
        atlas_id=parsed_atlas_id,
        name=asset_name,
        type=type,
        prompt="",
        aspect_ratio=aspect_ratio or DEFAULT_ASPECT_RATIO,
        resolution=final_resolution or DEFAULT_RESOLUTION,
        nine_slice={"l": 32, "t": 32, "r": 32, "b": 32} if sliced else None,
        reference_images=[],
        source="manual",
    )
    db.add(asset)
    db.commit()

    # Written only once the image has decoded and processed cleanly, and only once the
    # asset exists — its folder is where both files go, so there is nothing to write into
    # before the row is there, and a rejected drop leaves no orphan file behind.
    processed = new_asset_path(db, asset, asset_name)
    out.save(processed)
    raw = new_asset_path(db, asset, f"{asset_name}-raw", ext)
    raw.write_bytes(data)

    version = AssetVersion(
        asset_id=asset.id,
        provider="manual",
        composed_prompt="",
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


