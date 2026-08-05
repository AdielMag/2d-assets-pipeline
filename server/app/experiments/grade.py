"""Automatic grading of one sweep result.

Every metric here is reference-based where ground truth exists and reference-free only
where it cannot. The distinction matters: `score_asset` already answers "does this still
look like what was on screen", which is the failure mode of both steps (the model redesigns
instead of reproducing). What it cannot answer is "did Polish actually make anything
sharper" — a pixel-perfect copy of a blurry crop scores 100 — so sharpness is measured
separately, against the image the model was given rather than against the truth.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from ..processing.fidelity import ALPHA_CUTOFF, score_asset
from ..processing.fidelity import _foreground_estimate  # noqa: PLC2701 - deliberate reuse
from ..processing.transparency import _magenta_score  # noqa: PLC2701 - deliberate reuse

# Mirrors of `transparency.remove_background`'s own thresholds, so "would the keyer fire"
# is answered by the keyer's actual rule rather than by a second, drifting one. Kept as
# named constants here because this module asks a different question of them — not "should
# I key" but "did the model give us something keyable".
KEY_SCORE = 160.0        # magenta_frac is measured above this
RESIDUE_SCORE = 195.0    # near-pure key pixels, counted for the small-patch clause
MIN_MAGENTA_FRAC = 0.02  # ...or MIN_RESIDUE_PX of the above, matching the `or` in the keyer
MIN_RESIDUE_PX = 4
# A surviving opaque pixel scoring above the keyer's `opaque_at` ramp start is tinted
# toward the key — the pink rim that shows up once the sprite is drawn in Unity.
FRINGE_SCORE = 115.0


def _alpha_mask(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGBA"))[..., 3] > ALPHA_CUTOFF


def _chroma_health(img: Image.Image) -> tuple[float, bool]:
    """(fraction of pixels on the key, would the keyer fire) for a raw provider output.

    Scored the way the keyer scores — red and blue both exceeding green — rather than by
    distance to #FF00FF, so gold and red artwork are not mistaken for key colour."""
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    score = _magenta_score(arr)
    frac = float((score > KEY_SCORE).mean())
    fires = frac > MIN_MAGENTA_FRAC or int((score > RESIDUE_SCORE).sum()) >= MIN_RESIDUE_PX
    return frac, fires


def _magenta_residue(img: Image.Image) -> float:
    """Fraction of the *surviving* sprite still tinted toward the key — the fringe the
    strip left behind, which shows up as a pink rim once Unity draws the sprite."""
    rgba = np.asarray(img.convert("RGBA"))
    opaque = rgba[..., 3] > ALPHA_CUTOFF
    if not opaque.any():
        return 0.0
    score = _magenta_score(rgba[..., :3].astype(np.float32))
    return float((score[opaque] > FRINGE_SCORE).mean())


def _halo(img: Image.Image) -> float:
    """Share of the sprite's own pixels that are partially transparent. A crisp cut is
    nearly all-or-nothing; a soft skirt of the old backdrop shows up here."""
    alpha = np.asarray(img.convert("RGBA"))[..., 3]
    own = alpha > ALPHA_CUTOFF
    if not own.any():
        return 0.0
    return float(((alpha > ALPHA_CUTOFF) & (alpha < 240))[own].mean())


def _sharpness(img: Image.Image, size: tuple[int, int]) -> float | None:
    """Edge energy (variance of Laplacian) with both images normalised to one size.

    Normalising first is the whole point: measured at native resolution, any model that
    returns a bigger canvas scores higher for free, whether or not it resolved real detail.
    Brought to a common size, a genuinely sharper redraw keeps its edge energy and an
    upscaled blur does not."""
    import cv2

    rgba = img.convert("RGBA").resize(size, Image.LANCZOS)
    arr = np.asarray(rgba)
    mask = arr[..., 3] > ALPHA_CUTOFF
    if mask.sum() < 64:
        return None
    gray = np.asarray(Image.fromarray(arr[..., :3]).convert("L"), dtype=np.float64)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    return float(lap[mask].var())


def _boxes_mask(shape: tuple[int, int], boxes: list[list[int]]) -> np.ndarray | None:
    mask = np.zeros(shape, bool)
    for b in boxes:
        x0, y0, x1, y1 = (int(v) for v in b)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(shape[1], x1), min(shape[0], y1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask if mask.any() else None


def _tight(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box of a boolean mask."""
    rows, cols = np.any(mask, axis=1), np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return None
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    return int(x0), int(y0), int(x1) + 1, int(y1) + 1


def _grade_isolation(truth: Image.Image, sprite: Image.Image) -> dict:
    """Grade an isolated caption sprite against the caption's own crop.

    `score_asset` is wrong for this one: the truth crop still has the button surface behind
    the lettering while a correct sprite is transparent there, so a colour comparison would
    punish exactly the right answer. What is asked instead is whether the sprite's alpha
    covers the same *letter shapes* as the reference and nothing else, so it is graded as a
    mask agreement problem.

    Both masks are cropped to their own bounding box and resampled to a common size before
    comparison. Without that, this measures framing rather than lettering: the sprite is
    trimmed to content and padded to the caption's aspect ratio, so its glyphs run
    edge-to-edge, while the truth crop's glyphs sit inside the label box with padding around
    them. Measured on visually near-perfect output from two different models, the unaligned
    comparison returned ~0.45 IoU and ranked both below an untouched crop of the whole
    button — it was scoring the offset, not the letters.
    """
    truth_rgb = np.asarray(truth.convert("RGB"))
    ink = _foreground_estimate(truth_rgb)
    # Two views of the sprite's alpha, for two different questions. `alpha_at_truth` shares
    # the truth crop's pixel grid, which is what `spill` needs to ask "is anything painted
    # where the reference has no ink". `alpha_full` is native and only feeds the bounding
    # box, so glyph shape is compared without resampling the sprite twice.
    alpha_full = _alpha_mask(sprite)
    alpha_at_truth = _alpha_mask(sprite.convert("RGBA").resize(truth.size, Image.LANCZOS))
    out = {
        "ink_iou": None, "ink_recall": None, "spill": None,
        "coverage": round(float(alpha_at_truth.mean()), 4),
    }
    if ink is None or not alpha_full.any():
        return out

    ib, ab = _tight(ink), _tight(alpha_full)
    if ib is None or ab is None:
        return out
    size = (256, 128)

    def _norm(mask: np.ndarray, box) -> np.ndarray:
        crop = Image.fromarray((mask * 255).astype(np.uint8)).crop(box).resize(size, Image.NEAREST)
        return np.asarray(crop) > 127

    ink_n, alpha_n = _norm(ink, ib), _norm(alpha_full, ab)
    inter = np.logical_and(ink_n, alpha_n).sum()
    union = np.logical_or(ink_n, alpha_n).sum()
    out["ink_iou"] = round(float(inter / union), 4) if union else 0.0
    out["ink_recall"] = round(float(inter / max(1, ink_n.sum())), 4)
    # Painted where there is no ink — the "it drew the button behind the letters too"
    # failure. Measured on the truth grid rather than the normalised boxes, since an opaque
    # sprite spills across the whole frame regardless of how its bounding box lines up.
    out["spill"] = round(
        float((alpha_at_truth & ~ink).sum() / max(1, alpha_at_truth.sum())), 4
    )
    return out


def grade(job, out_img: Image.Image, raw_img: Image.Image | None) -> dict:
    """All automatic metrics for one finished job. `raw_img` is the provider's output
    before background removal, needed because chroma-key success can only be judged on the
    image that still has the key in it."""
    truth = Image.open(job.truth_path).convert("RGB")
    result: dict = {"kind": job.kind}

    # Chroma health — a hard gate, judged on the raw output.
    if raw_img is not None:
        frac, fires = _chroma_health(raw_img)
        result["magenta_frac"] = round(frac, 4)
        result["chroma_ok"] = fires
    result["magenta_residue"] = round(_magenta_residue(out_img), 4)
    result["halo"] = round(_halo(out_img), 4)

    if job.kind == "text_isolate":
        result.update(_grade_isolation(truth, out_img))
        iou = result.get("ink_iou")
        result["headline"] = round(100.0 * iou, 2) if iou is not None else None
        return result

    ignore = None
    if job.kind == "text_remove" and job.label_boxes:
        # Excuse the lettering's own pixels: the truth crop still has the text, the output
        # is supposed not to. Without this the step is scored down for succeeding. What the
        # excusal does NOT do is make the area a free-for-all — `score_asset`'s residue term
        # measures leftover glyph fragments inside exactly these boxes.
        ignore = _boxes_mask((truth.size[1], truth.size[0]), job.label_boxes)

    scored = score_asset(
        truth, out_img, fit=job.fit, nine_slice=job.nine_slice,
        background_rgb=tuple(job.background_rgb) if job.background_rgb else None,
        ignore=ignore,
    )
    result.update(scored)
    result["headline"] = scored.get("score")

    if job.kind == "text_remove" and scored.get("residue") is not None:
        # `score_asset` weights residue at 0.07 — correct for a general fidelity number,
        # where leftover glyphs are one defect among many. Here it is half the job: the
        # Text step exists to make the lettering go away, and the other half is not
        # damaging the element while doing it. Measured on a realistic caption, the
        # unweighted score separates "erased" from "text fully intact" by 1.8 points,
        # which cannot rank models; at 50/50 the same pair separates by ~12.
        #
        # Residue cannot tell WHICH glyphs survived — a wrong word left behind scores like
        # the right one — so character-level correctness is left to the vision judge.
        result["headline"] = round(
            0.5 * scored["score"] + 50.0 * (1.0 - scored["residue"]), 2
        )

    # Did the redraw actually resolve detail, or just restate the reference? Only
    # meaningful for polish, which is the step that asks for it.
    if job.kind == "polish":
        ref = Image.open(job.ref_path)
        before = _sharpness(ref, truth.size)
        after = _sharpness(out_img, truth.size)
        result["sharpness_before"] = round(before, 2) if before else None
        result["sharpness_after"] = round(after, 2) if after else None
        result["sharpness_gain"] = (
            round(after / before, 3) if before and after and before > 0 else None
        )
    return result
