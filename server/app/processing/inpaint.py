"""De-occlusion: remove the things drawn on top of an element, so the element underneath
can be extracted whole.

A panel, bar or button in a screenshot arrives with whatever sat on top of it baked in —
the PLAY button comes with its sword, the nav bar with its five icons, the profile banner
with the avatar capping its end. That is faithful to the reference but not *reusable*: the
frame can't be restyled, re-labelled, 9-sliced to a different width, or shared by a
template family while a sword is painted into the middle of it.

**This works on the screenshot, not on the extracted sprite, and that is the whole design.**

The previous version did the opposite — extract the frame, then paint holes in the sprite —
and every defect it produced followed from that ordering:

* The occluder's *bounding box* was the only mask available, because the sprite had already
  been cut and the occluder's real shape was gone. Erasing rectangles is what put the
  staircase of square notches along the top edge of the nav bar sprite.
* Where an occluder overhung the frame, the frame's alpha there was the *occluder's*
  silhouette, so something had to guess what shape the frame really was. Three rules were
  tried (extrapolate the outline, give up and go transparent, keep the surviving frame's
  bounding box) and each one broke a different sprite.
* The colour behind the occluder had to be invented from the frame's own rows and columns,
  which smears the moment the frame is not a clean 1-D gradient.

Removing the occluders from the screenshot first deletes all three problems rather than
solving them. The frame's true silhouette becomes directly visible, so segmentation reads
it off the picture instead of inferring it; the colour behind the occluder is inpainted with
the whole screenshot for context; and the mask is the occluder's own alpha, which has no
corners in it. `_repair_alpha`, `_directional_fill` and their thousand lines of tuning are
gone, and so are the artefacts they existed to manage.

LaMa is the inpainter, run at native resolution over the full screenshot. That is also what
SPRITE (CHI 2026) does — "an inpainting model performs occlusion recovery, filling
background gaps left by extracted layers" — and it is the configuration LaMa is good at:
run on a ~90px crop with transparent pixels flattened to grey, as this module used to, it
has no context and answers with hallucinated photographic texture. Telea is the fallback
when torch is unavailable.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .. import ml

__all__ = [
    "deocclude", "boxes_to_mask", "alpha_to_mask", "plausible_fill", "dilate_mask",
]

# Grow each occluder by this fraction of its size before inpainting. A mask taken from an
# extracted sprite's alpha is tight to the icon, so its antialiased rim and drop shadow fall
# just outside — left unmasked they survive as a halo of icon-coloured pixels ringing the
# hole, which reads worse than the icon did.
_DILATE_FRAC = 0.10
_DILATE_MIN = 3

# Above this fraction of the area masked there is not enough surrounding context to infer
# from and the inpainter starts inventing. Keep the occluded original instead — it is at
# least the real thing.
_MAX_MASK_FRAC = 0.72

# Alpha at or below this is not part of the element.
_ALPHA_CUTOFF = 16

# A mask whose deepest point is within this many pixels of known content is "thin" — Telea
# territory. Dilated text strokes sit around 4-8px; a lifted icon is 20-60.
_THIN_MAX = 10.0

# Plausibility gate on an inpainted fill, measured against the content ringing the hole.
# Generous on purpose: it is here to reject invented texture, not to grade quality.
_PLAUSIBLE_MEAN_DIST = 55.0
_PLAUSIBLE_STD_RATIO = 2.5
_PLAUSIBLE_STD_FLOOR = 14.0


def boxes_to_mask(
    size: tuple[int, int], boxes: list[tuple[int, int, int, int]], dilate: bool = True
) -> np.ndarray:
    """Rasterise boxes into a uint8 mask (255 = inpaint), clipped to `size`.

    The crude fallback, for an occluder that has not been extracted yet and so has no alpha
    of its own. Prefer `alpha_to_mask`: a rectangle erases the frame's corners along with
    the icon, and on a frame edge that shows.
    """
    w, h = size
    mask = np.zeros((h, w), dtype=np.uint8)
    for x0, y0, x1, y1 in boxes:
        if dilate:
            gx = max(_DILATE_MIN, int((x1 - x0) * _DILATE_FRAC))
            gy = max(_DILATE_MIN, int((y1 - y0) * _DILATE_FRAC))
            x0, y0, x1, y1 = x0 - gx, y0 - gy, x1 + gx, y1 + gy
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 255
    return mask


def alpha_to_mask(
    size: tuple[int, int], sprite: Image.Image, box: tuple[int, int, int, int]
) -> np.ndarray:
    """Stamp an extracted sprite's own alpha into a screen-sized mask at `box`.

    This is what replaces erasing bounding boxes. An occluder has already been extracted by
    the time its parent frame is built, so its exact silhouette is known — the sword's
    diagonal, the shield's point, the avatar's circle — and erasing that shape leaves the
    frame's own geometry untouched, where erasing its rectangle takes a bite out of the
    frame's edge in the shape of a box.
    """
    w, h = size
    mask = np.zeros((h, w), np.uint8)
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return mask
    resized = sprite.convert("RGBA").resize((x1 - x0, y1 - y0), Image.LANCZOS)
    mask[y0:y1, x0:x1] = (np.asarray(resized)[:, :, 3] > _ALPHA_CUTOFF).astype(np.uint8) * 255
    return mask


def dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    """Grow a mask to cover the antialiased rim and drop shadow around what it marks."""
    import cv2

    if radius <= 0 or not mask.any():
        return mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    return cv2.dilate(mask, k, iterations=1)


def _lama(rgb: np.ndarray, mask: np.ndarray, progress=None) -> np.ndarray:
    lama = ml.get_lama(progress=progress)
    out = lama(Image.fromarray(rgb), Image.fromarray(mask))
    result = np.asarray(out.convert("RGB"))
    # SimpleLama pads to a multiple of 8 internally and does not always crop back.
    return result[: rgb.shape[0], : rgb.shape[1]]


def _telea(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    import cv2

    # Telea's radius is how far it looks for evidence. Fixed at 3 it under-fills a thick
    # stroke, leaving a faint core of the original glyph; scaling with the hole lets a
    # heavy display caption be reached from real content on both sides.
    radius = int(min(12, max(3, round(_mask_thickness(mask)))))
    bgr = cv2.inpaint(rgb[:, :, ::-1].copy(), mask, radius, cv2.INPAINT_TELEA)
    return bgr[:, :, ::-1]


def _mask_thickness(mask: np.ndarray) -> float:
    """Distance from the deepest masked pixel to the nearest unmasked one — how far the
    inpainter has to reach before it runs out of real evidence."""
    import cv2

    binary = (mask > 0).astype(np.uint8)
    if not binary.any():
        return 0.0
    return float(cv2.distanceTransform(binary, cv2.DIST_L2, 3).max())


def plausible_fill(
    painted: np.ndarray, mask: np.ndarray, region_box: tuple[int, int, int, int] | None = None
) -> bool:
    """Does the filled area look like what surrounds it?

    Catches a backend inventing content: LaMa's photographic texture on flat UI shading
    shows up as fill whose mean colour and variance are nothing like the picture
    immediately around the hole. This is a sanity gate, not a quality score — it rejects
    obvious garbage and lets everything else through.

    `region_box` narrows the "surrounds it" ring to the parent frame's own pixels when an
    occluder sits at that frame's edge. An icon nearly as tall as the pill it caps (the
    common case — that is exactly why it needed erasing) leaves a ring that is mostly
    OUTSIDE the frame — sky, not pill — so the unrestricted ring agrees with whatever the
    backend painted even when it painted sky-and-a-distant-flag into what should have been
    the pill's own rounded end. Restricting the reference pixels to the frame's own box
    (when it contributes enough of them) fixes the comparison to what the fill is actually
    supposed to match.
    """
    import cv2

    holes = mask > 0
    if not holes.any():
        return True
    holes_u8 = holes.astype(np.uint8)

    ring = None
    restricted = False
    if region_box is not None:
        x0, y0, x1, y1 = region_box
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(mask.shape[1], x1), min(mask.shape[0], y1)
        in_region = np.zeros(mask.shape, bool)
        if x1 > x0 and y1 > y0:
            in_region[y0:y1, x0:x1] = True
        # Grow the skirt until it has real frame pixels to compare against, instead of
        # taking a fixed 6px ring and falling straight back to the UNRESTRICTED ring (which
        # readily includes whatever is outside the frame) the moment that ring mostly lands
        # outside it. An occluder sitting at a frame's own edge is the common case — a gem
        # capping the end of its pill, a badge hanging off a banner's corner (see
        # _occluding_regions) — and 6px from an edge-hugging hole is often mostly outside
        # the frame already. Widening the search while staying clipped to the frame finds
        # the frame's own pixels instead of settling for the sky beside it.
        for iters in (6, 12, 20, 32):
            candidate = (
                cv2.dilate(holes_u8, np.ones((3, 3), np.uint8), iterations=iters) > 0
            ) & ~holes & in_region
            if candidate.sum() >= 32:
                ring, restricted = candidate, True
                break
    if ring is None:
        ring = (cv2.dilate(holes_u8, np.ones((3, 3), np.uint8), iterations=6) > 0) & ~holes
    if ring.sum() < 32:
        return True

    fill = painted[holes].astype(np.float32)
    around = painted[ring].astype(np.float32)
    # A ring restricted to the frame's own pixels is a reliable reference — it is not
    # comparing against "whatever happens to be nearby", it is comparing against the exact
    # thing the fill is supposed to continue. That earns a tighter tolerance: measured on
    # a gem icon LaMa filled with a pale, washed-out patch, the unrestricted threshold
    # (55) passed a fill sitting 35 apart from its own pill's true colour — plausible only
    # because 55 was sized for the noisier unrestricted case. 0.6 (33) still clears a good
    # Telea text fill (measured 3-30 apart from its own frame across six real regions).
    #
    # A structure/edge-energy check was tried here too and removed: it compared the fill's
    # local gradient against the ring's, on the theory that a real patch of frame carries
    # about as much detail as its surroundings. That is backwards for TEXT — a correctly
    # erased caption is SUPPOSED to come back flat, that is what "removed" looks like, and
    # the ring (borders, icons, other glyphs) is naturally high-energy — so it rejected
    # every clean text fill measured (ratios of 0.03-0.17 against a 0.35 floor) and let the
    # one actual defect it was meant to catch (a noisy LaMa hallucination) straight through,
    # since invented photographic texture has plenty of its own edge energy.
    mean_limit = _PLAUSIBLE_MEAN_DIST * (0.6 if restricted else 1.0)
    if float(np.linalg.norm(fill.mean(axis=0) - around.mean(axis=0))) > mean_limit:
        return False
    return float(fill.std()) <= _PLAUSIBLE_STD_RATIO * float(around.std()) + _PLAUSIBLE_STD_FLOOR


def deocclude(
    rgb: np.ndarray, mask: np.ndarray, method: str = "auto", progress=None,
    region_box: tuple[int, int, int, int] | None = None,
) -> tuple[np.ndarray, str]:
    """Paint the masked area of a screenshot out, returning (rgb, method_used).

    `rgb` is the whole screenshot, not a crop, and that is the point — LaMa reconstructs
    from context, and a full frame carries far more of it than a 90-pixel cutout does. The
    caller then segments the element out of the result as if the occluder had never been
    there.

    `method` is "auto" (LaMa, falling back to Telea), "lama" or "telea". The returned name
    is what actually ran — "none" when the mask was empty, too large, or every backend
    failed — so callers can record it rather than assume.

    `region_box` is the parent frame's own pixel box, passed through to `plausible_fill` — see
    there for why an occluder mask needs it. Optional and irrelevant when the hole in
    question is not "an icon at the edge of the thing it sits on" (text runs, for example).
    """
    work = (mask > 0).astype(np.uint8) * 255
    if not work.any():
        return rgb, "none"
    if work.astype(bool).mean() > _MAX_MASK_FRAC:
        return rgb, "none"

    if method == "auto":
        # Telea diffuses isotropically, so it is only right where every painted pixel is a
        # pixel or two from real content — a hairline text stroke. Anything wider converges
        # to the average of its surroundings and comes back a smudge, which is LaMa's job.
        order = ("telea",) if _mask_thickness(work) <= _THIN_MAX else ("lama", "telea")
    else:
        order = (method,) if isinstance(method, str) else tuple(method)

    backends = {"lama": lambda: _lama(rgb, work, progress), "telea": lambda: _telea(rgb, work)}
    for backend in order:
        try:
            painted = backends[backend]()
        except Exception as e:
            if progress:
                progress(f"{backend} inpainting failed ({e})")
            continue
        # Only gate a backend that has a fallback behind it; the last one is the answer.
        if backend != order[-1] and not plausible_fill(painted, work, region_box):
            if progress:
                progress(f"{backend} filled the hole with something implausible; retrying")
            continue
        return painted, backend

    # Every backend failed. The occluded screenshot is still truthful, so hand it back
    # rather than failing the whole asset over a reusability improvement.
    return rgb, "none"
