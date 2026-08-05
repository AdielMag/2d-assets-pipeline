"""Fidelity scoring: how close is a produced asset to the reference it came from?

Every asset in this pipeline originates from a rectangle on a source screenshot. That
crop is the ground truth. This module renders the asset back into that rectangle exactly
the way the compositor would (same 9-slice / contain / stretch math, reused from
`composite.py` rather than reimplemented) and measures the difference.

Three complementary numbers, because no single one is sufficient:

* **delta_e** — mean CIEDE2000 colour distance. Catches hue/saturation drift, which is
  the dominant failure of regenerating an element instead of extracting it (a navy pill
  coming back teal scores badly here while still matching structurally).
* **ssim** — structural similarity on luma. Catches shape, edge and layout error that a
  colour metric is blind to (right colours, wrong bevel).
* **alpha_iou** — overlap between the asset's alpha and a foreground estimate of the
  crop. Catches a silhouette that is too fat (background baked in) or too thin (the
  element's glow/shadow clipped off).

`score` folds them into one 0-100 number so runs can be compared and ranked. The
weighting favours colour and structure; alpha is a smaller term because the foreground
estimate it compares against is itself approximate.
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from skimage.color import deltaE_ciede2000, rgb2lab
from skimage.metrics import structural_similarity

from .composite import _contain_resize, nine_slice_resize

# A delta_e at or above this counts as a total colour miss (score contribution 0).
# CIEDE2000 is calibrated so ~1 is a just-noticeable difference and ~5 is "clearly a
# different colour"; 25 is far past "wrong".
DELTA_E_FLOOR = 25.0

# Pixels this transparent are treated as not part of the asset at all.
ALPHA_CUTOFF = 16

_WEIGHTS = {"color": 0.36, "structure": 0.30, "alpha": 0.09, "clean": 0.18, "residue": 0.07}

# How close an opaque pixel must be to the surrounding backdrop colour to count as
# background the extractor failed to cut away. Tight, so genuinely-similar element colours
# aren't mistaken for bleed.
_BLEED_DIST = 26.0

# Bleed below _BLEED_TOL is free, at or above _BLEED_SAT scores zero. A cut sprite always
# carries some backdrop in its antialiased rim, so a linear penalty charges correct
# extractions for physics while barely separating them from a total failure to cut — the
# uncut rectangle is the one defect this metric exists to catch, and it needs a cliff.
_BLEED_TOL = 0.06
_BLEED_SAT = 0.45

# Softens the residue ratio on frames that are near-uniform to begin with, where a tiny
# absolute change would otherwise read as a huge proportional one.
_RESIDUE_FLOOR = 8.0


def render_like_compositor(
    asset_img: Image.Image, target_w: int, target_h: int,
    fit: str = "stretch", nine_slice: dict | None = None,
) -> Image.Image:
    """Place `asset_img` into a target_w x target_h box the same way `composite_layout`
    would, so a score reflects what the screen will actually look like rather than the
    raw sprite. Mirrors the fit branches in `composite.composite_layout`."""
    target_w, target_h = max(1, target_w), max(1, target_h)
    rgba = asset_img.convert("RGBA")

    if fit == "slice" and nine_slice:
        return nine_slice_resize(rgba, nine_slice, target_w, target_h)

    if fit == "contain":
        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        resized, ox, oy = _contain_resize(rgba, target_w, target_h)
        canvas.alpha_composite(resized, (ox, oy))
        return canvas

    return rgba.resize((target_w, target_h), Image.LANCZOS)


def fit_mode_for(asset_type: str, nine_slice: dict | None) -> str:
    """The fit an asset of this type will be drawn with. Matches the branch in
    `routers.mockups.render_preview_screen` so scores and previews never disagree."""
    if nine_slice is not None and asset_type not in ("tile", "sprite_sheet"):
        return "slice"
    if asset_type in ("icon", "symbol", "glyph", "character"):
        return "contain"
    return "stretch"


def _foreground_estimate(crop_rgb: np.ndarray) -> np.ndarray | None:
    """Rough boolean foreground mask of a reference crop, used only as the comparison
    target for alpha_iou. The crop is a tight box around one element, so its border ring
    is mostly whatever surrounded the element; pixels far from that ring's median colour
    are foreground. Deliberately cheap — the real segmentation lives in `extract.py`.

    Returns None when the estimate is degenerate (almost nothing or almost everything
    separates from the border colour). That happens on crops with no real figure/ground
    contrast, where the mask carries no information — scoring against it would dock a
    perfectly good asset for failing to match noise, so the caller drops the term instead
    of pretending to measure it."""
    h, w = crop_rgb.shape[:2]
    ring = max(1, min(h, w) // 16)
    border = np.concatenate([
        crop_rgb[:ring].reshape(-1, 3), crop_rgb[-ring:].reshape(-1, 3),
        crop_rgb[:, :ring].reshape(-1, 3), crop_rgb[:, -ring:].reshape(-1, 3),
    ]).astype(np.float32)
    bg = np.median(border, axis=0)
    dist = np.linalg.norm(crop_rgb.astype(np.float32) - bg, axis=2)
    fg = dist > 32.0
    frac = float(fg.mean())
    if frac < 0.02 or frac > 0.98:
        return None
    return fg


def _blend_onto(rgba: np.ndarray, backdrop: np.ndarray) -> np.ndarray:
    """Alpha-composite an RGBA array over an opaque RGB backdrop, returning RGB float.
    Scoring has to happen on composited pixels: comparing a transparent sprite's raw RGB
    against an opaque crop would measure the arbitrary colour sitting under alpha=0."""
    alpha = (rgba[..., 3:4].astype(np.float32)) / 255.0
    return rgba[..., :3].astype(np.float32) * alpha + backdrop.astype(np.float32) * (1 - alpha)


def _residue(rendered_arr: np.ndarray, ignore: np.ndarray | None) -> float | None:
    """Excess pixel variation inside an excused (text) area versus the frame around it.

    0 means the cleared area is as smooth as its surroundings; 1 means it is far busier,
    which is what leftover glyph fragments or invented texture look like. Returns None
    when there is nothing excused or too little surrounding frame to compare against.
    """
    if ignore is None or not ignore.any():
        return None
    alpha = rendered_arr[..., 3] > ALPHA_CUTOFF
    inside = ignore & alpha
    outside = (~ignore) & alpha
    if inside.sum() < 16 or outside.sum() < 64:
        return None
    rgb = rendered_arr[..., :3].astype(np.float32)
    ref_std = float(rgb[outside].std())
    got_std = float(rgb[inside].std())
    excess = (got_std - ref_std) / (ref_std + _RESIDUE_FLOOR)
    return float(min(1.0, max(0.0, excess)))


def score_asset(
    crop: Image.Image, asset_img: Image.Image,
    fit: str = "stretch", nine_slice: dict | None = None,
    background_rgb: tuple[float, float, float] | None = None,
    ignore: np.ndarray | None = None,
) -> dict:
    """Score one produced asset against the reference crop it was derived from.

    Returns {delta_e, ssim, alpha_iou, bg_bleed, coverage, score}, score 0-100 (higher is
    better). `coverage` is the fraction of the box the asset actually paints — a sprite
    that only fills 60% of its slot is reported rather than silently scoring well on the
    part it does cover.

    Pass `background_rgb` (the median colour just *outside* the region box) whenever it is
    available. Without it the metric cannot tell a correctly cut-out sprite from an uncut
    opaque rectangle: the rectangle reproduces the reference crop exactly — it *is* the
    reference crop — so it wins on colour and structure while being useless as a sprite,
    since it bakes in a slab of backdrop that shows the moment Unity draws it anywhere
    else. `bg_bleed` measures precisely that failure and the score charges for it.

    `ignore` is a boolean mask of pixels the sprite is not answerable for — in practice
    the text runs, which are deliberately inpainted off the frame so Unity can render
    them with a real font. Without it the metric penalises the pipeline for doing the
    right thing, and every text-removal improvement reads as a regression.
    """
    crop_rgb_img = crop.convert("RGB")
    tw, th = crop_rgb_img.size
    crop_rgb = np.asarray(crop_rgb_img)

    rendered = render_like_compositor(asset_img, tw, th, fit=fit, nine_slice=nine_slice)
    rendered_arr = np.asarray(rendered)

    asset_alpha = rendered_arr[..., 3] > ALPHA_CUTOFF
    coverage = float(asset_alpha.mean())

    # Nothing was drawn — a fully failed asset. Report it rather than dividing by zero.
    if not asset_alpha.any():
        return {
            "delta_e": float(DELTA_E_FLOOR), "ssim": 0.0, "alpha_iou": 0.0,
            "bg_bleed": None, "residue": None, "coverage": 0.0, "score": 0.0,
        }

    # Composite both onto the same neutral backdrop so alpha is handled identically.
    backdrop = np.full_like(crop_rgb, 128)
    rendered_rgb = _blend_onto(rendered_arr, backdrop)
    ref_rgb = crop_rgb.astype(np.float32)

    # How much high-frequency mess is left in the excused area. `ignore` says "the sprite
    # is not answerable for reproducing the text here" — it must not also say "anything
    # goes here", or a botched erase that leaves letter-shaped blotches scores as well as
    # a clean one. It did exactly that once: an inpainting backend filled the lettering
    # with invented texture and the mean score went UP, because the damage was inside the
    # excused pixels. Cleared text should look like the frame around it, so residue is
    # measured as excess local variation against the frame just outside the mask.
    residue = _residue(rendered_arr, ignore)

    if ignore is not None and ignore.shape == asset_alpha.shape and ignore.any():
        # Blank the excused pixels to the SAME value in both images. SSIM is windowed and
        # cannot simply skip pixels, so making the two agree there is what keeps an
        # excused area from dragging the structure term down; the colour and cleanliness
        # terms drop those pixels from their means outright.
        ref_rgb = ref_rgb.copy()
        rendered_rgb = rendered_rgb.copy()
        ref_rgb[ignore] = 128.0
        rendered_rgb[ignore] = 128.0
        asset_alpha = asset_alpha & ~ignore
        if not asset_alpha.any():
            return {
                "delta_e": 0.0, "ssim": 1.0, "alpha_iou": None,
                "bg_bleed": None, "residue": residue, "coverage": coverage,
                "score": 100.0 * (1.0 - (residue or 0.0)),
            }

    # --- colour: CIEDE2000 over the pixels the asset actually paints ---
    lab_ref = rgb2lab(ref_rgb / 255.0)
    lab_out = rgb2lab(rendered_rgb / 255.0)
    de_map = deltaE_ciede2000(lab_ref, lab_out)
    delta_e = float(np.mean(de_map[asset_alpha]))

    # --- structure: SSIM on luma over the whole box ---
    ref_gray = np.asarray(
        Image.fromarray(ref_rgb.astype(np.uint8)).convert("L"), dtype=np.float32
    )
    out_gray = np.asarray(
        Image.fromarray(rendered_rgb.astype(np.uint8)).convert("L"), dtype=np.float32
    )
    win = min(7, tw if tw % 2 else tw - 1, th if th % 2 else th - 1)
    if win >= 3:
        ssim = float(structural_similarity(ref_gray, out_gray, data_range=255.0, win_size=win))
    else:
        ssim = 0.0

    # --- alpha: silhouette agreement against a rough foreground estimate ---
    fg = _foreground_estimate(crop_rgb)
    alpha_iou = None
    if fg is not None and ignore is not None and ignore.shape == fg.shape:
        fg = fg & ~ignore
    if fg is not None:
        union = np.logical_or(fg, asset_alpha).sum()
        alpha_iou = float(np.logical_and(fg, asset_alpha).sum() / union) if union else 0.0

    # --- cleanliness: how much of what the sprite paints is actually backdrop ---
    bg_bleed = None
    if background_rgb is not None:
        bg = np.asarray(background_rgb, dtype=np.float32)
        dist = np.linalg.norm(ref_rgb - bg, axis=2)
        bg_bleed = float((dist[asset_alpha] < _BLEED_DIST).mean())

    color_term = max(0.0, 1.0 - delta_e / DELTA_E_FLOOR)
    structure_term = max(0.0, min(1.0, (ssim + 1.0) / 2.0))  # SSIM is [-1, 1]

    terms = [(_WEIGHTS["color"], color_term), (_WEIGHTS["structure"], structure_term)]
    if alpha_iou is not None:
        terms.append((_WEIGHTS["alpha"], alpha_iou))
    if bg_bleed is not None:
        excess = (bg_bleed - _BLEED_TOL) / (_BLEED_SAT - _BLEED_TOL)
        terms.append((_WEIGHTS["clean"], 1.0 - min(1.0, max(0.0, excess))))
    if residue is not None:
        terms.append((_WEIGHTS["residue"], 1.0 - residue))
    # Renormalize over whichever terms were measurable, so dropping an unmeasurable
    # term rescales the rest instead of capping the achievable score.
    total_w = sum(w for w, _ in terms)
    score = 100.0 * sum(w * v for w, v in terms) / total_w

    return {
        "delta_e": round(delta_e, 3),
        "ssim": round(ssim, 4),
        "alpha_iou": round(alpha_iou, 4) if alpha_iou is not None else None,
        "bg_bleed": round(bg_bleed, 4) if bg_bleed is not None else None,
        "residue": round(residue, 4) if residue is not None else None,
        "coverage": round(coverage, 4),
        "score": round(score, 2),
    }


def score_composite(
    reference: Image.Image, composited: Image.Image, target: np.ndarray | None = None
) -> dict:
    """Whole-screen score: the rebuilt screen against the source screenshot.

    `composited` must have a TRANSPARENT background — pass the screen render with
    `bg="#00000000"`, not the checkerboard the UI preview uses, or the score measures the
    checkerboard's grey against the game art instead of measuring the reconstruction.

    Quality (ΔE, SSIM) is measured only over pixels the pipeline actually produced, and
    the area it failed to produce is charged separately through `filled`. So an
    unfinished screen is penalised by an explicit, principled coverage term rather than
    by whatever colour happens to sit behind the holes.

    `target` is a boolean mask of the area the run was actually trying to rebuild — the
    union of the region rects. Without it `filled` is measured against the whole canvas,
    which conflates two completely different things: how well the pipeline reproduced what
    it set out to reproduce, and how much of the screen anyone asked it to. A perfect
    reconstruction of every detected element scored 18/100 purely because the backdrop and
    the hero had not been detected yet, so the headline number could not be read as good
    or bad. Scoped to `target`, 100 means "everything in scope came out right" and
    `screen_filled` reports the scope separately.
    """
    ref = reference.convert("RGB")
    out = composited.convert("RGBA")
    if out.size != ref.size:
        out = out.resize(ref.size, Image.LANCZOS)

    ref_arr = np.asarray(ref)
    out_arr = np.asarray(out)
    covered = out_arr[..., 3] > ALPHA_CUTOFF
    screen_filled = float(covered.mean())
    if target is not None and target.shape == covered.shape and target.any():
        filled = float(covered[target].mean())
        in_scope = float(target.mean())
    else:
        filled = screen_filled
        in_scope = 1.0

    if not covered.any():
        return {"delta_e": float(DELTA_E_FLOOR), "ssim": 0.0, "filled": 0.0,
                "screen_filled": 0.0, "in_scope": round(in_scope, 4),
                "quality": 0.0, "score": 0.0}

    # Flatten the produced pixels over the reference itself: where the pipeline drew
    # something we compare it to the truth; where it drew nothing the two agree by
    # construction and are excluded from the masked statistics below anyway.
    out_rgb = _blend_onto(out_arr, ref_arr)

    de_map = deltaE_ciede2000(rgb2lab(ref_arr / 255.0), rgb2lab(out_rgb / 255.0))
    delta_e = float(de_map[covered].mean())

    ref_gray = np.asarray(ref.convert("L"), dtype=np.float32)
    out_gray = np.asarray(Image.fromarray(out_rgb.astype(np.uint8)).convert("L"), dtype=np.float32)
    _, ssim_map = structural_similarity(ref_gray, out_gray, data_range=255.0, full=True)
    ssim = float(ssim_map[covered].mean())

    color_term = max(0.0, 1.0 - delta_e / DELTA_E_FLOOR)
    structure_term = max(0.0, min(1.0, (ssim + 1.0) / 2.0))
    quality = 100.0 * (0.5 * color_term + 0.5 * structure_term)
    return {
        "delta_e": round(delta_e, 3),
        "ssim": round(ssim, 4),
        # Of the area in scope, how much the pipeline actually painted.
        "filled": round(filled, 4),
        # Of the whole canvas — the honest reminder of how much is still out of scope.
        "screen_filled": round(screen_filled, 4),
        "in_scope": round(in_scope, 4),
        # Quality of what was drawn, ignoring holes.
        "quality": round(quality, 2),
        # Headline: quality discounted by how much of the in-scope area was rebuilt.
        "score": round(quality * filled, 2),
    }
