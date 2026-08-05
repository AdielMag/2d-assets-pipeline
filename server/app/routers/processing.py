import io

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from ..db import get_db
from ..processing.nine_slice import detect_borders
from ..processing.tiling import tile_preview
from ..processing.transparency import has_real_alpha, remove_flat_background
from ..processing.trim import trim_for_fit, trim_to_content
from ..schemas import AssetOut
from ..storage import abs_path, new_asset_path, rel_path
from .assets import get_asset_or_404

router = APIRouter(prefix="/api", tags=["processing"])


def _selected_version(asset):
    for v in asset.versions:
        if v.id == asset.selected_version_id:
            return v
    return asset.versions[-1] if asset.versions else None


@router.post("/assets/{asset_id}/detect-nine-slice")
def detect_nine_slice(asset_id: int, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    version = _selected_version(asset)
    if not version:
        raise HTTPException(400, "Asset has no generated version yet")
    # Detect on the exact image that gets exported (not a discarded trimmed copy), so
    # the borders always line up with the PNG Unity imports.
    with Image.open(abs_path(version.processed_path or version.raw_path)) as img:
        borders = detect_borders(img)
        width, height = img.width, img.height
    asset.nine_slice = borders
    db.commit()
    return {"nine_slice": borders, "width": width, "height": height}


@router.post("/assets/{asset_id}/trim", response_model=AssetOut)
def trim_asset(asset_id: int, db: Session = Depends(get_db)):
    """Trim the selected version's processed image to its content bounding box, then
    re-detect borders so they stay in sync with the new (smaller) image."""
    asset = get_asset_or_404(db, asset_id)
    version = _selected_version(asset)
    if not version:
        raise HTTPException(400, "Asset has no generated version yet")
    src = abs_path(version.processed_path or version.raw_path)
    with Image.open(src) as img:
        trimmed = trim_to_content(img)
        dest = new_asset_path(db, asset, f"{asset.name}-trim")
        trimmed.save(dest)
        if asset.nine_slice is not None:
            asset.nine_slice = detect_borders(trimmed)
    version.processed_path = rel_path(dest)
    db.commit()
    db.refresh(asset)
    return asset


# Small on purpose: the keying cost is dominated by the matting solve, which runs at the
# image's own size once that is under transparency.MATTE_MAX_DIM. This is a card thumbnail.
PREVIEW_MAX_DIM = 500


@router.post("/cutout-preview")
async def cutout_preview(
    file: UploadFile, trim: bool = Form(True), sliced: bool = Form(False),
):
    """Key the backdrop out of an uploaded image and hand the result straight back as a
    PNG, touching neither the DB nor storage. Lets the Import tab show what the cutout will
    look like *before* the user commits it as an asset — the same keying pass
    `/projects/{id}/assets/import-cutout` will run.

    Downscaled first: a 4MP source takes ~7s to key, almost all of it in the matting solve,
    and this result is only ever shown in a thumbnail. The import itself keys the full-size
    original, so nothing about the saved asset is limited by this."""
    try:
        with Image.open(io.BytesIO(await file.read())) as img:
            if max(img.size) > PREVIEW_MAX_DIM:
                scale = PREVIEW_MAX_DIM / max(img.size)
                img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
            out = img.convert("RGBA") if has_real_alpha(img) else remove_flat_background(img)
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Could not read that image file")
    if trim:
        out = trim_for_fit(out, sliced)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@router.get("/assets/{asset_id}/tile-preview")
def asset_tile_preview(asset_id: int, db: Session = Depends(get_db)):
    asset = get_asset_or_404(db, asset_id)
    version = _selected_version(asset)
    if not version:
        raise HTTPException(400, "Asset has no generated version yet")
    with Image.open(abs_path(version.processed_path or version.raw_path)) as img:
        preview = tile_preview(img)
    buf = io.BytesIO()
    preview.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")
