"""Ties the fidelity metrics to the data model: score an asset against the region it
came from, and score a whole rebuilt screen against its source screenshot.

Kept out of `processing/` (which stays pure image-in/image-out and dependency-free of
the ORM) and out of the routers (so the CLI in `tools/score_run.py` can use it too).
"""
from __future__ import annotations

from PIL import Image
from sqlalchemy.orm import Session

from .models import Asset, AssetVersion, Mockup, MockupRegion
from .processing.fidelity import fit_mode_for, score_asset, score_composite
from .storage import abs_path

__all__ = [
    "region_crop_image", "score_version_against_region", "rescore_region",
    "score_mockup", "score_mockup_screen", "target_mask", "label_mask",
]


def region_crop_image(mockup: Mockup, region: MockupRegion) -> Image.Image | None:
    """The reference pixels for a region: its rectangle cut out of the source screen.
    Returns a detached copy so the caller doesn't hold the screenshot open."""
    src = abs_path(mockup.image_path)
    if not src.exists():
        return None
    with Image.open(src) as img:
        w, h = img.size
        box = (
            int(w * region.x / 100), int(h * region.y / 100),
            int(w * (region.x + region.w) / 100), int(h * (region.y + region.h) / 100),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        return img.convert("RGB").crop(box)


def _version_image(version: AssetVersion) -> Image.Image | None:
    path = abs_path(version.processed_path or version.raw_path)
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.copy()


def score_version_against_region(
    version: AssetVersion, asset: Asset, mockup: Mockup, region: MockupRegion
) -> dict | None:
    """Score one asset version against the region rect it was derived from, rendering it
    the way the compositor will actually draw it (9-slice / contain / stretch)."""
    crop = region_crop_image(mockup, region)
    img = _version_image(version)
    if crop is None or img is None:
        return None
    return score_asset(
        crop, img,
        fit=fit_mode_for(asset.type, asset.nine_slice),
        nine_slice=asset.nine_slice,
    )


def rescore_region(db: Session, mockup: Mockup, region: MockupRegion) -> dict | None:
    """Score a region's bound asset **in isolation** against its crop, and persist the
    result. Used for immediate feedback at generation time, when no full-screen composite
    exists yet.

    Note this under-scores background frames on purpose-built decompositions: a PlayButton
    frame is deliberately generated empty while its crop still contains the sword and the
    'PLAY' text, so the difference is charged against the frame even though the pipeline
    is doing the right thing. `score_mockup` uses the composited screen instead, which
    layers the icon back on and is the authoritative number."""
    if region.asset_id is None:
        return None
    asset = db.get(Asset, region.asset_id)
    if asset is None:
        return None
    version = asset.selected_version
    if version is None:
        return None

    result = score_version_against_region(version, asset, mockup, region)
    if result is not None:
        result["method"] = "isolated"
        version.fidelity = result
        db.commit()
    return result


def _region_box(region: MockupRegion, w: int, h: int) -> tuple[int, int, int, int]:
    return (
        int(w * region.x / 100), int(h * region.y / 100),
        int(w * (region.x + region.w) / 100), int(h * (region.y + region.h) / 100),
    )


def surrounding_color(reference: Image.Image, box: tuple[int, int, int, int]):
    """Median colour of a ring just outside a region box — the backdrop the element sits
    on. Feeds `score_asset`'s bg_bleed term, which is the only thing that distinguishes a
    properly cut sprite from an uncut rectangle (both match the reference inside the box).
    Returns None when the box is flush against the image edges on all sides."""
    import numpy as np

    arr = np.asarray(reference.convert("RGB"))
    H, W = arr.shape[:2]
    x0, y0, x1, y1 = box
    mx = max(4, int((x1 - x0) * 0.15))
    my = max(4, int((y1 - y0) * 0.15))
    ex0, ey0 = max(0, x0 - mx), max(0, y0 - my)
    ex1, ey1 = min(W, x1 + mx), min(H, y1 + my)

    patch = arr[ey0:ey1, ex0:ex1].reshape(-1, 3)
    inner = arr[y0:y1, x0:x1].reshape(-1, 3)
    if patch.shape[0] <= inner.shape[0]:
        return None
    # Ring = expanded patch minus the box. Summing then differencing the histograms would
    # be exact, but a median over the concatenated border strips is enough and cheaper.
    strips = []
    if ey0 < y0:
        strips.append(arr[ey0:y0, ex0:ex1].reshape(-1, 3))
    if y1 < ey1:
        strips.append(arr[y1:ey1, ex0:ex1].reshape(-1, 3))
    if ex0 < x0:
        strips.append(arr[y0:y1, ex0:x0].reshape(-1, 3))
    if x1 < ex1:
        strips.append(arr[y0:y1, x1:ex1].reshape(-1, 3))
    if not strips:
        return None
    ring = np.concatenate(strips).astype(np.float32)
    return tuple(float(v) for v in np.median(ring, axis=0))


def target_mask(mockup: Mockup, w: int, h: int):
    """Boolean mask of the screen area this mockup is trying to rebuild — the union of its
    region rects.

    The whole-screen score is normalised by this. Measured against the full canvas instead,
    the headline number is dominated by whatever nobody has asked the pipeline to produce
    yet: a flawless reconstruction of all 15 detected elements read as 18/100 because the
    backdrop and the hero make up four fifths of the pixels. Scoped to the regions, the
    number answers the question actually being asked — how good is the reconstruction of
    what we set out to reconstruct — and `screen_filled` keeps the other question visible
    rather than folding the two into one unreadable figure."""
    import numpy as np

    mask = np.zeros((h, w), bool)
    for region in mockup.regions:
        x0, y0, x1, y1 = _region_box(region, w, h)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask if mask.any() else None


def label_mask(mockup: Mockup, box: tuple[int, int, int, int], w: int, h: int):
    """Boolean mask over `box` marking the pixels covered by text runs.

    Handed to `score_asset` as `ignore`. Text is inpainted off the sprites on purpose, so
    the reference's lettering has no counterpart in the reconstruction and charging the
    sprite for the difference would score the pipeline down for working as designed."""
    import numpy as np

    labels = getattr(mockup, "labels", None)
    if not labels:
        return None
    x0, y0, x1, y1 = box
    mask = np.zeros((y1 - y0, x1 - x0), bool)
    for label in labels:
        lx0, ly0 = int(w * label.x / 100.0), int(h * label.y / 100.0)
        lx1, ly1 = int(w * (label.x + label.w) / 100.0), int(h * (label.y + label.h) / 100.0)
        ix0, iy0 = max(x0, lx0) - x0, max(y0, ly0) - y0
        ix1, iy1 = min(x1, lx1) - x0, min(y1, ly1) - y0
        if ix1 > ix0 and iy1 > iy0:
            mask[iy0:iy1, ix0:ix1] = True
    return mask if mask.any() else None


def score_mockup(db: Session, mockup: Mockup, include_screen: bool = True) -> dict:
    """Score a screen by rendering it once and comparing each region's rectangle of the
    result against the same rectangle of the source screenshot.

    Scoring off the composite rather than off individual sprites is what makes the
    numbers fair: background/foreground decomposition, template families (shared frame +
    per-member icon), mirrored reuse and 9-slice stretching are all applied by the
    renderer, so a region is judged on what the user will actually see there — not on a
    frame that is empty by design. It also means one render serves every region instead
    of re-compositing per region."""
    src = abs_path(mockup.image_path)
    reference = None
    composite = None
    if src.exists():
        with Image.open(src) as im:
            reference = im.convert("RGB").copy()
        composite = _render_transparent(db, mockup)

    regions = []
    scored = []
    for region in mockup.regions:
        entry = {
            "region_id": region.id,
            "name": region.name,
            "asset_id": region.asset_id,
            "type": region.asset_type,
            "fidelity": None,
        }
        result = None
        if region.asset_id is not None and reference is not None and composite is not None:
            box = _region_box(region, reference.width, reference.height)
            if box[2] > box[0] and box[3] > box[1]:
                # No `ignore=label_mask(...)`: text erasure is off by default now, so the
                # lettering is present in both the reference and the reconstruction and
                # excusing it would only hide real differences. `label_mask` stays for when
                # the toggle is on — there, charging a frame for missing text it was told
                # to drop scores the pipeline down for working as designed.
                result = score_asset(
                    reference.crop(box), composite.crop(box), fit="stretch", nine_slice=None,
                    background_rgb=surrounding_color(reference, box),
                )
                result["method"] = "composite"
                asset = db.get(Asset, region.asset_id)
                version = asset.selected_version if asset else None
                if version is not None:
                    version.fidelity = result
        if result is not None:
            entry["fidelity"] = result
            scored.append(result["score"])
        regions.append(entry)
    db.commit()

    out = {
        "mockup_id": mockup.id,
        "regions": regions,
        "bound": len(scored),
        "total": len(regions),
        "mean_score": round(sum(scored) / len(scored), 2) if scored else None,
        "min_score": round(min(scored), 2) if scored else None,
    }

    if include_screen and reference is not None and composite is not None:
        out["screen"] = score_composite(
            reference, composite, target=target_mask(mockup, reference.width, reference.height)
        )
        out["screen"]["missing"] = [
            r.name for r in mockup.regions if r.asset_id is None
        ]
    return out


def score_mockup_screen(db: Session, mockup: Mockup) -> dict | None:
    """Whole-screen score only, without the per-region breakdown."""
    src = abs_path(mockup.image_path)
    if not src.exists():
        return None
    composite = _render_transparent(db, mockup)
    if composite is None:
        return None
    with Image.open(src) as im:
        ref = im.convert("RGB")
        return score_composite(
            ref, composite, target=target_mask(mockup, ref.width, ref.height)
        )


def _render_transparent(db: Session, mockup: Mockup) -> Image.Image | None:
    """The mockup's bound assets composited on a fully transparent canvas.

    Transparent, not the UI's checkerboard: scoring the checkerboard's grey against the
    game's artwork would measure a UI affordance instead of the reconstruction. Imported
    lazily because `routers.mockups` imports this module's siblings — a module-scope
    import would close the cycle."""
    from .routers.mockups import render_preview_screen

    try:
        preview = render_preview_screen(db, mockup, bg="#00000000")
    except Exception:
        return None
    path = abs_path(preview["path"])
    if not path.exists():
        return None
    with Image.open(path) as im:
        return im.convert("RGBA").copy()
