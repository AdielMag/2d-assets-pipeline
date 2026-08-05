"""Cut an element out of a screenshot instead of asking a model to redraw it.

This is the inversion the pipeline is built around. A screenshot already contains the
exact pixels of every element on it, so for anything not occluded, segmentation gives
100% colour and shape fidelity for free and offline — where regenerating gives a
plausible lookalike that drifts in hue and proportion, and costs provider quota.

Three stages, and the order matters more than any of them individually:

1. **Segment.** Box-prompted SAM 2 on the (supersampled) screenshot, with GrabCut as the
   offline fallback. SAM2 leads because it is measurably better on the case that defines
   this problem — a small element drawn ON a large flat one. GrabCut's definite-foreground
   seed is an inset *rectangle*, so on a narrow diagonal object that seed is mostly the
   frame underneath, its colour model learns the frame, and the frame comes back inside the
   mask: the PLAY button's sword segments at 48% coverage carrying a slab of blue, against
   SAM2's 29.5% of sword and nothing else.
2. **Matte.** Closed-form alpha matting over a trimap derived from the mask, then Germer's
   multi-level foreground estimation to recover the element's own colour. See `_matte`.
3. **Grow, if the box was clipping.** Unchanged, and still the only stage that may return a
   different rect than it was given.

Everything here is pure image-in/image-out — no ORM, no config — so it stays testable
with synthetic inputs and reusable from the CLI tools. Supersampling is deliberately NOT
done here: the caller passes an already-enlarged screenshot as `source`, because one
screenshot feeds fifteen regions and the model pass should be paid once (see
`upres.supersample_cached`).
"""
from __future__ import annotations

import warnings

import numpy as np
from PIL import Image

# Background samples come from a ring *outside* the region box, never from inside it.
#
# This is the single most important choice in this module. A detected region box is a
# TIGHT bound on the element, so an element's own border sits right at the box edge —
# seeding any part of the box interior as background teaches GrabCut that the border is
# background and it cuts the frame off, leaving a PLAY button with no gold bevel and a
# nav bar reduced to its highlight tab. Real background pixels only exist outside the
# box, so that is where they are sampled from.
_CONTEXT_MARGIN = 0.18   # ring outside the box, as a fraction of box size
_CONTEXT_MIN_PX = 6      # ...at scale 1; scaled with the supersample factor
# Inner core seeded as definite foreground. Generous: the box is tight, so its middle is
# reliably the element.
_FG_INSET = 0.28

# A mask covering more than this much of the box means the segmenter absorbed the
# surrounding background; less than this means it found only a fragment. Either way the
# result is not a usable element.
_MASK_MAX = 0.995
_MASK_MIN = 0.12

# Box growth. A side counts as clipped when this much of its border line is foreground;
# each round pushes that ONE side out by _GROW_STEP of the box's span on that axis, never
# more than _GROW_MAX cumulatively, and the growth is kept only if at least
# _GROW_MIN_GAIN of the newly-added strip comes back as element.
#
# One side at a time, verified on that side's own strip. Growing every clipped side
# together and testing the joint gain is what let the bottom bevel come off the PLAY
# button and the tips off the nav-bar swords: a tight box on a rectangular element reads
# as clipped on all four sides (its own border line IS foreground), three of the four
# grow into background, and their zero gain averages the one real side below threshold —
# so nothing grows and the genuinely-clipped side stays clipped.
#
# The thresholds are deliberately lenient, because the two errors are not symmetric.
# Growing a box that did not need it costs a few transparent pixels around a sprite that
# is composited by its rect anyway; failing to grow one that did permanently amputates
# part of the element. _strip_gain is the real guard, so the trigger can afford to be
# jumpy: a sword tip only occupies a slice of the strip it is found in.
_CLIP_FRAC = 0.12
_GROW_STEP = 0.07
_GROW_MIN_PX = 3         # ...at scale 1
_GROW_MAX = 0.35
_GROW_MIN_GAIN = 0.05
_GROW_ROUNDS = 3
_GROW_BG_TOL = 30.0   # RGB euclidean from the sampled backdrop; nearer than this is not element
# ...and the grown mask must still be the SAME element: this much of what was selected
# before the box moved has to survive the re-segmentation. See `_retained`.
_GROW_KEEP_FRAC = 0.75

# Self-assessment: a finished mask that still reads as substantially the surrounding
# backdrop has not found the element, whatever backend produced it — it has grabbed a
# neighbour it happens to touch (a badge sitting on a tower spire, a coin overlapping the
# pill it caps), or the whole box outright (a hollow arrow over a solid-coloured highlight,
# where "the object" and "the highlight" are the same colour so nothing looks wrong to a
# coverage check). Same colour-distance test as `_strip_gain`'s growth guard, just run on
# the WHOLE finished mask instead of one growth strip, because this failure isn't specific
# to growth — GoldCoinIcon bled without ever growing, and EventsNavIcon's trophy fell back
# to a solid rectangle with no growth involved either.
#
# The response mirrors what a human fixing one of these by hand actually does: try it
# without growth (undoes a neighbour getting pulled in as the box crept outward), then try
# it a bit tighter still (gives the segmenter less of the neighbour to have an opinion
# about). Whichever candidate bleeds least, including the original, wins — with one gate,
# on the tighter-than-requested rung only: it must land UNDER `_BLEED_RETRY_FRAC` to be
# accepted at all. Ranking alone silently assumes the score is measuring an intruder, and
# when it isn't, "lowest score" degenerates to "smallest box". See `_pick_candidate`.
_BLEED_RETRY_FRAC = 0.10   # more than this share of the mask reading as backdrop triggers retries
_BLEED_SHRINK_STEP = 0.10  # the tightened retry's box, as a fraction pulled in from detect_box
# ...but an element's own OUTLINE is not an intruder, and `_foreign_blob_frac` cannot tell
# the two apart on the properties it was reading. A gold keyline around a blue panel is
# off-colour from the panel (that is what makes it a keyline), is one connected component
# (it is a ring), and touches the box border on every side (the panel is drawn flush to its
# tight box) — so it scored 0.18 on the pipeline's single most common element shape and put
# every bordered button through the shrink ladder for no reason.
#
# What separates them is shape, not colour. A neighbour riding along is a compact chunk
# glued to one part of the border: its bounding box covers a fraction of the mask's, and it
# fills that box solidly. An outline is a shell: its bounding box IS the mask's bounding box
# on both axes, and it fills almost none of it. Both conditions are required, so a large
# solid intruder that happens to stretch across the mask is still caught.
_SHELL_SPAN = 0.9   # spans at least this much of the mask's bbox on BOTH axes...
_SHELL_FILL = 0.5   # ...while filling less than this much of its own bbox
# Growth's own gain check (`_strip_gain`) only looks at the newly-added strip, so it cannot
# see a mask trading a small bleed for a large one elsewhere — which is exactly what
# happened the first time a hollow arrow got fixed: the mask's own outline now touched the
# box border (`_clipped_sides` fires on a thin ring almost by construction), growth
# re-segmented the bigger box, SAM2 answered with the whole highlight again, and the fix
# undid itself one build later. Below, growth must not accept a trial whose OVERALL bleed
# is meaningfully worse than what it already had — the same measure the retry ladder uses,
# so the two can never fight each other back and forth across rebuilds.
_BLEED_GROW_TOL = 0.02

# Half-width of the trimap's unknown band, in SOURCE pixels — scaled with the supersample
# factor, so the solver always sees the same physical strip of screenshot however much it
# was enlarged. Two pixels covers a screenshot's own antialiasing plus a pixel of mask
# error. Wider is not safer: every extra pixel is one more the solver is free to reassign,
# and on a small icon a generous band reaches clean across a stroke.
_TRIMAP_BAND = 2

# Crop area above which the matte is solved on a reduced copy — see `_solve_matte_scaled`.
# Closed-form matting's system covers every pixel, so its cost follows the crop's total
# area and not the trimap's band: measured at x4, 0.84MP solves in 5.7s, 4.1MP in 24.5s and
# 23.9MP in 309s. This cap is the knee of that curve, and it only ever engages on elements
# that span most of the screen, whose export target is far smaller than the crop anyway.
_MATTE_MAX_PIXELS = 4_000_000

# `shadow_margin=True` extends the crop this far past the hard mask, so a soft cast shadow
# or an outer keyline stroke — real pixels, just not part of SAM2's binary object mask —
# has somewhere to be recovered from instead of being cropped away before matting ever sees
# it. Mostly a fixed pixel amount, not a fraction of box size: this UI style's shadow/keyline
# reads as a roughly constant stroke width regardless of how big the element is, so scaling
# the margin with the box (measured first at 14% — a large button pulled in over a quarter
# of its own width, far past where the shadow actually ends) way overshoots on anything
# bigger than an icon. `_SHADOW_MARGIN_FRAC` only matters as a small nudge on genuinely
# large frames, capped by `_SHADOW_MARGIN_MAX_PX`. Kept well clear of `_TRIMAP_BAND`'s tight
# interior band (untouched by this) specifically because that band's own comment is a
# warning earned the hard way: this margin only widens the OUTER unknown ring, in
# `_shadow_trimap`, never the inner one, so a thin stroke deep inside the element is never
# at risk from it.
_SHADOW_MARGIN_FRAC = 0.025
_SHADOW_MARGIN_MIN_PX = 10
_SHADOW_MARGIN_MAX_PX = 22
# Outermost strip of the widened crop asserted as definite background. Kept as a hard floor
# even though `_shadow_trimap` now asserts everything past the shadow band anyway: a mask
# that runs to the edge of its own box would otherwise dilate its unknown band clean off the
# canvas and leave the solver no anchor at all on that side.
_SHADOW_EDGE_PX = 2


class ExtractionFailed(RuntimeError):
    """No backend produced a usable mask for this box."""


def _mask_is_degenerate(mask: np.ndarray) -> bool:
    """Whether a mask is too big, too small, or too fragmented to be one element."""
    import cv2

    frac = float(mask.mean())
    if frac > _MASK_MAX or frac < _MASK_MIN:
        return True
    # One element should be essentially one blob. Many disconnected specks means the
    # segmenter latched onto texture rather than the object.
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    if n <= 1:
        return True
    areas = sorted(stats[1:, cv2.CC_STAT_AREA], reverse=True)
    return areas[0] < 0.55 * float(mask.sum())


def _clean_mask(mask: np.ndarray, scale: int = 1) -> np.ndarray:
    """Close pinholes, drop specks, keep the largest connected component, restore the
    element's own enclosed interior.

    Which gaps count as "enclosed interior" is decided on the mask as the segmenter left
    it, BEFORE the morphological close — and that ordering is the whole of the rule.
    Colour cannot make this call: the social nav icon's shield is filled with exactly the
    blue of the bar it sits on, and the button showing through the V between a sword's
    blade and its guard is exactly the same blue too. Both are gaps the segmenter left out
    of the mask; one belongs to the element and one does not.

    Geometry separates them cleanly, but only if asked in time. The shield's interior is
    landlocked. The sword's notch opens outward — until the close seals its mouth, after
    which an unconditional hole fill puts the button right back inside the sprite. Asked
    before the close, the notch is still open water and stays out.

    The structuring element scales with the supersample factor so a x4 mask is smoothed by
    the same *physical* amount as a native one; a fixed 3x3 would barely touch it, and
    raising the iteration count instead would round off genuine corners.
    """
    import cv2

    m = mask.astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    interior = np.zeros_like(m)
    if n > 1:
        biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        interior = _enclosed_holes((labels == biggest).astype(np.uint8))

    r = max(1, int(scale))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=2)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k, iterations=1)

    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        m = (labels == largest).astype(np.uint8)
    return (m | interior).astype(bool)


def _enclosed_holes(m: np.ndarray) -> np.ndarray:
    """Background pixels completely surrounded by the element (e.g. the dark centre of a
    ring) — these belong to the element and must be opaque. Background that reaches the
    image border does not, and must stay transparent.

    Reachability is tested from a synthetic one-pixel border added around the image, so
    the answer does not depend on whether the element happens to touch a corner. Seeding a
    flood at (0, 0) directly — as this did originally — silently inverted whenever the
    element occupied that corner, marking the entire background as an enclosed hole and
    returning a fully opaque mask. That is what put an opaque rectangle of sky behind
    every icon that ran to the edge of its box.
    """
    import cv2

    inv = (m == 0).astype(np.uint8)
    padded = cv2.copyMakeBorder(inv, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
    ff = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(padded, ff, (0, 0), 0)  # clear all border-reachable background
    return padded[1:-1, 1:-1]             # what survives is genuinely enclosed


def _backdrop_sample(
    full_rgb: np.ndarray, box_xyxy, scale: int = 1
) -> np.ndarray | None:
    """Median colour of a ring just outside the box — what the element sits on.

    Same reasoning as `classical_mask`'s seeding: real backdrop pixels only exist outside
    a tight detection box. Returns None when the box is flush against the image border on
    every side and there is no outside to sample."""
    H, W = full_rgb.shape[:2]
    x0, y0, x1, y1 = box_xyxy
    mn = _CONTEXT_MIN_PX * max(1, int(scale))
    mx = max(mn, int((x1 - x0) * _CONTEXT_MARGIN))
    my = max(mn, int((y1 - y0) * _CONTEXT_MARGIN))
    strips = []
    if y0 > 0:
        strips.append(full_rgb[max(0, y0 - my):y0, max(0, x0 - mx):min(W, x1 + mx)])
    if y1 < H:
        strips.append(full_rgb[y1:min(H, y1 + my), max(0, x0 - mx):min(W, x1 + mx)])
    if x0 > 0:
        strips.append(full_rgb[y0:y1, max(0, x0 - mx):x0])
    if x1 < W:
        strips.append(full_rgb[y0:y1, x1:min(W, x1 + mx)])
    pixels = [s.reshape(-1, 3) for s in strips if s.size]
    if not pixels:
        return None
    return np.median(np.concatenate(pixels).astype(np.float32), axis=0)


def classical_mask(
    full_bgr: np.ndarray, box_xyxy: tuple[int, int, int, int], scale: int = 1
) -> np.ndarray:
    """GrabCut over the box plus a ring of surrounding context, seeded so that only
    genuinely-outside-the-box pixels are treated as background.

    The offline fallback, not the primary path — see the module docstring for why it loses
    on any element drawn on top of another. It stays because it needs no model, no VRAM and
    no download, so a machine without torch still extracts something truthful.

    Returns a boolean mask cropped back to the box.
    """
    import cv2

    H, W = full_bgr.shape[:2]
    x0, y0, x1, y1 = box_xyxy
    bw, bh = x1 - x0, y1 - y0
    if bh < 8 or bw < 8:
        return np.ones((bh, bw), bool)

    # Expand outward for context; clamp at the image edge (an element flush against the
    # screen border legitimately has no outside on that side).
    mn = _CONTEXT_MIN_PX * max(1, int(scale))
    mx = max(mn, int(bw * _CONTEXT_MARGIN))
    my = max(mn, int(bh * _CONTEXT_MARGIN))
    ex0, ey0 = max(0, x0 - mx), max(0, y0 - my)
    ex1, ey1 = min(W, x1 + mx), min(H, y1 + my)
    patch = full_bgr[ey0:ey1, ex0:ex1]
    ph, pw = patch.shape[:2]

    # Box coordinates within the expanded patch.
    bx0, by0 = x0 - ex0, y0 - ey0
    bx1, by1 = bx0 + bw, by0 + bh

    gc = np.full((ph, pw), cv2.GC_BGD, np.uint8)   # the outer ring: definite background
    # A neutral collar between the definite-background ring and the box. Without it, an
    # element drawn flush to its box (a tight detection, which is the good case) has its
    # outermost pixels sitting directly against definite-background seeds. GrabCut's
    # smoothness term then drags them out regardless of colour — the gold border of a
    # button reads as neither the learned foreground (its blue interior, all the inset
    # core samples) nor the learned background (navy), so adjacency decides and the frame
    # loses its border. The collar is real background too, but only *probably* so, which
    # lets colour likelihood settle the border instead of proximity.
    cx0, cy0 = max(0, bx0 - mx // 2), max(0, by0 - my // 2)
    cx1, cy1 = min(pw, bx1 + mx // 2), min(ph, by1 + my // 2)
    gc[cy0:cy1, cx0:cx1] = cv2.GC_PR_BGD
    gc[by0:by1, bx0:bx1] = cv2.GC_PR_FGD           # the whole box: probably the element
    iy, ix = int(bh * _FG_INSET), int(bw * _FG_INSET)
    if by0 + iy < by1 - iy and bx0 + ix < bx1 - ix:
        gc[by0 + iy:by1 - iy, bx0 + ix:bx1 - ix] = cv2.GC_FGD  # core: definitely element

    # Degenerate when the box touches an image edge on every side — no context to learn
    # from, so fall back to keeping the box.
    if not (gc == cv2.GC_BGD).any():
        return np.ones((bh, bw), bool)

    try:
        cv2.grabCut(
            patch, gc, None,
            np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64),
            3, cv2.GC_INIT_WITH_MASK,
        )
    except cv2.error:
        return np.ones((bh, bw), bool)

    mask = np.isin(gc, (cv2.GC_FGD, cv2.GC_PR_FGD))
    return mask[by0:by1, bx0:bx1]


def sam2_mask(full_rgb: np.ndarray, box_xyxy: tuple[int, int, int, int]) -> np.ndarray:
    """Box-prompted ML segmentation over the FULL screenshot, returning a mask cropped to
    the box. Kept under its original name (and the `"sam2"` backend tag `best()` returns)
    for compatibility with stored `region.method` values and the auto/classical/sam2 API —
    what actually runs underneath has changed, see below.

    Prompting on the whole image rather than the crop matters: the segmenter uses
    surrounding context to decide where an object ends, and a pre-cropped image throws
    exactly that away — the element would run to the crop's edges with nothing to separate
    it from. Box prompt only, and the highest-scoring of the three candidates. Both were
    checked rather than assumed: the top-IoU candidate was the correct mask on every region
    of the ClashUp lobby (the other two are a fragment and an inversion), and adding
    negative point prompts at the centres of known overlapping siblings — the obvious way
    to tell it "not the badge, the avatar" — destroyed the mask instead, taking
    PlayerAvatar from 70.7% coverage to 9.9% of scattered fragments.

    HQ-SAM (vit_tiny), not SAM2, is the model actually asked: SAM2's decoder rounds off
    thin or hollow shapes into a blob — a crossed-blade icon's notch and tips came back
    fused shut no matter what was tried against it (box jitter ensembling, SAM2's own
    iterative mask_input refinement across three passes, hq_token_only) — all converged to
    the identical wrong shape, which is the signature of a decoder limit rather than an
    unlucky prompt. HQ-SAM's learnable high-quality output token resolves it, and scored no
    worse than SAM2 on every other region checked. SAM2 stays as the fallback if HQ-SAM's
    library or weights aren't available in a given environment.
    """
    from .. import ml

    try:
        predictor = ml.get_hqsam_predictor()
    except ml.MlUnavailable:
        predictor = ml.get_sam2_predictor()
    predictor.set_image(full_rgb)
    masks, scores, _ = predictor.predict(
        box=np.array(box_xyxy, dtype=np.float32)[None, :],
        multimask_output=True,
    )
    masks = np.asarray(masks).reshape(-1, *full_rgb.shape[:2])
    best = masks[int(np.argmax(np.asarray(scores).reshape(-1)))]
    x0, y0, x1, y1 = box_xyxy
    return best[y0:y1, x0:x1].astype(bool)


def _trimap(mask: np.ndarray, band: int) -> np.ndarray:
    """Certain-foreground / certain-background / unknown, from a binary mask.

    1.0 and 0.0 are the segmenter's answer eroded and dilated away from its own boundary;
    0.5 is the strip between, which is the only place the matting solver is allowed to
    have an opinion. Constraining it this tightly is what keeps the interior of a sprite
    exactly opaque and the outside exactly clear, instead of the haze a global solve
    produces when handed a loose trimap.
    """
    import cv2

    k = np.ones((3, 3), np.uint8)
    m = mask.astype(np.uint8)
    out = np.full(mask.shape, 0.5, np.float64)
    out[cv2.erode(m, k, iterations=band) > 0] = 1.0
    out[cv2.dilate(m, k, iterations=band) == 0] = 0.0
    return out


def _widen_for_shadow(
    box: tuple[int, int, int, int], mask: np.ndarray, W: int, H: int, scale: int
) -> tuple[tuple[int, int, int, int], np.ndarray, tuple[int, int]]:
    """Expand a finished box outward and place `mask` inside the bigger canvas, so there is
    room for a soft cast shadow or outer keyline the hard segmenter mask stopped short of.
    The added ring starts as all-background (0); `_shadow_trimap` is what hands it to the
    matting solver to actually resolve instead of asserting it.

    Returns the margin it used along with the new box, because that margin is exactly how
    far out a shadow can possibly have been given room to live — which is what sizes
    `_shadow_trimap`'s unknown band."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    lo, hi = _SHADOW_MARGIN_MIN_PX * scale, _SHADOW_MARGIN_MAX_PX * scale
    mx = max(lo, min(hi, int(bw * _SHADOW_MARGIN_FRAC)))
    my = max(lo, min(hi, int(bh * _SHADOW_MARGIN_FRAC)))
    nx0, ny0 = max(0, x0 - mx), max(0, y0 - my)
    nx1, ny1 = min(W, x1 + mx), min(H, y1 + my)
    placed = np.zeros((ny1 - ny0, nx1 - nx0), bool)
    ox0, oy0 = x0 - nx0, y0 - ny0
    placed[oy0:oy0 + bh, ox0:ox0 + bw] = mask
    return (nx0, ny0, nx1, ny1), placed, (mx, my)


def _shadow_trimap(mask: np.ndarray, inner_band: int, margin: tuple[int, int]) -> np.ndarray:
    """Certain-foreground eroded off the hard mask by the SAME tight band an ordinary
    extraction uses — full interior precision, unchanged. Unknown is a band that follows the
    mask's own outline outward by `margin`, which is precisely the room `_widen_for_shadow`
    made. Everything past that band is asserted background.

    The band has to FOLLOW THE MASK, and that is the whole of the rule. This originally left
    the entire widened crop unknown apart from a 2px ring at its far edge, on the reasoning
    that closed-form matting reads real pixel evidence and so a shadow would settle to
    partial alpha while unrelated backdrop settled to ~0 on its own. That reasoning holds
    for a button with clear space around it and fails completely for anything whose box is
    mostly not the element: on the Matchmaking screen's header text, the mask covered 38% of
    its box, so 70.5% of the trimap was unknown with one 2px background anchor at the far
    edge to constrain it. The solve is under-determined at that point, alpha drifts to
    mid-values across the whole open region, and the sprite comes out as a translucent print
    of the gold panel it was sitting on — 61% of its pixels at fractional alpha.

    Sizing the band to the margin keeps what the feature was for. A cast shadow or an outer
    keyline in this UI style falls within `_SHADOW_MARGIN_MAX_PX` of the object by
    construction — that constant is what decided how much room to make for it — so the
    shadow is still inside the unknown band and still gets resolved from real evidence. What
    changes is that the backdrop two hundred pixels away is now asserted rather than solved,
    which is both true and what makes the system well-posed.

    The inner band is untouched: it is still `_TRIMAP_BAND`-tight, so a thin stroke deep
    inside the element is never at risk from this.
    """
    import cv2

    k = np.ones((3, 3), np.uint8)
    m = mask.astype(np.uint8)
    radius = float(max(1, int(margin[0]), int(margin[1])))
    # Distance transform, not a dilate. The band is ~90px at x4, and morphology with a
    # kernel that size costs O(radius^2) per pixel — a 177x177 ellipse over a 5482x589 crop
    # does not finish. `distanceTransform` answers the same question ("how far is the
    # nearest mask pixel") in one linear pass. Isotropic on the larger of the two margins,
    # which is the generous direction: a band slightly wider than the room made for it can
    # only give a shadow more evidence to be found in, never less.
    dist = cv2.distanceTransform((m == 0).astype(np.uint8), cv2.DIST_L2, 3)
    out = np.zeros(mask.shape, np.float64)
    out[(m > 0) | (dist <= radius)] = 0.5
    out[cv2.erode(m, k, iterations=inner_band) > 0] = 1.0
    edge = max(1, int(_SHADOW_EDGE_PX))
    out[:edge, :] = 0.0
    out[-edge:, :] = 0.0
    out[:, :edge] = 0.0
    out[:, -edge:] = 0.0
    return out


def _solve_matte(
    crop_rgb: np.ndarray, trimap: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Fractional alpha AND decontaminated colour, given a trimap.

    A screenshot's edge pixels are a real blend: the renderer wrote `I = aF + (1-a)B` for
    some coverage `a`, element colour `F` and backdrop `B`. Recovering `a` is a matting
    problem, and it has a right answer — closed-form matting (Levin et al.) solves the
    whole unknown band as one sparse linear system under a local colour-line prior, so
    neighbouring pixels constrain each other.

    That coupling is the point. This replaces a hand-rolled per-pixel projection onto a
    line between two box-blurred local means, which had no spatial prior at all and so
    failed pixel by pixel: the shop icon's awning came back with the backdrop showing
    through it, and the level badge's gold ring came back broken, with 17.7% of its opaque
    pixels stuck at partial alpha.

    Solving for `a` is only half of it. The edge pixel's RGB is still the *blend*, so a
    sprite cut this way carries a rim tinted with the screenshot's own backdrop and shows a
    coloured fringe the moment it is composited anywhere else. `estimate_foreground_ml`
    (Germer et al., Fast Multi-Level Foreground Estimation) recovers `F` and propagates it
    outward into the transparent region — which matters twice over here, because
    `fit_to_resolution` resizes RGBA non-premultiplied, so whatever colour sits under
    alpha=0 gets averaged back into the edge on the way down to the export size. Leaving
    the old backdrop there would reintroduce the fringe at the last step.

    Fully-opaque pixels keep the screenshot's own colour rather than the estimate: inside
    the element there is nothing to decontaminate and the reference pixels are the truth.

    Above `_MATTE_MAX_PIXELS` the solve is done on a reduced copy — see `_solve_matte_scaled`.
    """
    unknown = trimap == 0.5
    if not unknown.any():
        return (trimap >= 1.0).astype(np.float32), crop_rgb
    if trimap.size > _MATTE_MAX_PIXELS:
        return _solve_matte_scaled(crop_rgb, trimap)
    return _solve_matte_cf(crop_rgb, trimap)


def _solve_matte_cf(
    crop_rgb: np.ndarray, trimap: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """The closed-form solve itself, with no size dispatch — see `_solve_matte`.

    Kept separate precisely so `_solve_matte_scaled` can reach the solver without going back
    through the size check: rounding the reduced dimensions can land on the same area it
    started from, and re-entering the dispatcher there recurses forever.
    """
    image = crop_rgb.astype(np.float64) / 255.0
    try:
        from pymatting import estimate_alpha_cf, estimate_foreground_ml

        with warnings.catch_warnings():
            # pymatting warns "Thresholded incomplete Cholesky decomposition failed" when
            # the preconditioner is not positive-definite for a given crop. It falls back
            # internally and still converges; surfacing it would put noise in the SSE log.
            warnings.simplefilter("ignore")
            alpha = estimate_alpha_cf(image, trimap)
            foreground = estimate_foreground_ml(image, alpha)
    except Exception:
        # No pymatting, a singular system, an unusable crop — the binary mask is a worse
        # answer but still a truthful one, and an asset is better than an exception.
        return (trimap >= 1.0).astype(np.float32), crop_rgb

    alpha = np.clip(np.asarray(alpha, np.float32), 0.0, 1.0)
    # Re-assert the trimap. Closed-form matting constrains known pixels with a large
    # weight rather than exactly, so they come back at 0.998 rather than 1 — enough to
    # leave a sprite's interior imperceptibly translucent and its outside imperceptibly
    # opaque, which compounds over a 9-slice stretch.
    alpha[trimap == 1.0] = 1.0
    alpha[trimap == 0.0] = 0.0

    rgb = np.clip(np.asarray(foreground, np.float32) * 255.0, 0, 255).astype(np.uint8)
    rgb = np.where((alpha >= 1.0)[:, :, None], crop_rgb, rgb)
    return alpha, rgb


def _solve_matte_scaled(
    crop_rgb: np.ndarray, trimap: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """`_solve_matte` on a reduced copy, with the hard regions re-asserted at full size.

    Closed-form matting builds one sparse system over EVERY pixel, not just the unknown
    band, so its cost tracks the crop's total area regardless of how tight the trimap is. A
    frame-like element spanning most of the screen crops to ~24 megapixels at x4 and takes
    over five minutes; the same element's export target is a couple of thousand pixels wide.

    Only the fractional band actually needs the solver, and a fraction is a smooth quantity
    — halving the resolution it is computed at costs sub-pixel accuracy on an edge that is
    about to be downsampled anyway. What must NOT be approximated is which pixels are
    certainly in and certainly out, and those are not: they come straight back from the
    full-resolution trimap below, exactly as the unscaled path asserts them. So the interior
    stays exactly opaque and the exterior exactly clear at full resolution, and only the
    narrow ring between them is interpolated.

    Nearest-neighbour for the trimap, deliberately: it is a three-valued label image, and
    any averaging filter invents labels between 0, 0.5 and 1 that mean nothing.
    """
    import cv2

    h, w = trimap.shape
    scale = (_MATTE_MAX_PIXELS / float(h * w)) ** 0.5
    # floor, not round: rounding can land back on the original area, and the reduced copy
    # must be strictly smaller for this to be a bound at all.
    sh, sw = max(1, int(h * scale)), max(1, int(w * scale))

    small_rgb = cv2.resize(crop_rgb, (sw, sh), interpolation=cv2.INTER_AREA)
    small_tri = cv2.resize(trimap, (sw, sh), interpolation=cv2.INTER_NEAREST)
    if not (small_tri == 0.5).any():
        # The band was thin enough to vanish at this scale. Nothing left to solve, and the
        # binary answer is the honest one rather than a fabricated gradient.
        return (trimap >= 1.0).astype(np.float32), crop_rgb

    small_alpha, small_rgb_out = _solve_matte_cf(small_rgb, small_tri)

    alpha = cv2.resize(small_alpha.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    alpha = np.clip(alpha, 0.0, 1.0)
    alpha[trimap == 1.0] = 1.0
    alpha[trimap == 0.0] = 0.0

    rgb = cv2.resize(small_rgb_out, (w, h), interpolation=cv2.INTER_LINEAR)
    rgb = np.where((alpha >= 1.0)[:, :, None], crop_rgb, rgb).astype(np.uint8)
    return alpha, rgb


def _matte(
    crop_rgb: np.ndarray, mask: np.ndarray, band: int
) -> tuple[np.ndarray, np.ndarray]:
    """`_solve_matte` off the standard tight trimap — see there for the matting itself."""
    return _solve_matte(crop_rgb, _trimap(mask, band))


def _to_rgba(
    crop_rgb: np.ndarray, mask: np.ndarray, feather: bool = True, band: int = _TRIMAP_BAND
) -> Image.Image:
    """Apply a mask as alpha, matted to the element's true antialiased edge."""
    if feather and min(mask.shape) > 4 * band:
        alpha, rgb = _matte(crop_rgb, mask, band)
    else:
        alpha, rgb = mask.astype(np.float32), crop_rgb
    out = np.dstack([rgb, np.rint(np.clip(alpha, 0, 1) * 255).astype(np.uint8)])
    return Image.fromarray(out, "RGBA")


def _clipped_sides(mask: np.ndarray) -> list[str]:
    """Sides whose border row/column is mostly foreground — the element runs off the edge
    of its box there, so the box is very likely cutting it in half."""
    if mask.size == 0:
        return []
    edges = {
        "top": mask[0, :], "bottom": mask[-1, :],
        "left": mask[:, 0], "right": mask[:, -1],
    }
    return [side for side, line in edges.items() if float(line.mean()) >= _CLIP_FRAC]


def _backdrop_bleed(
    mask: np.ndarray, crop_rgb: np.ndarray, backdrop_rgb: np.ndarray | None
) -> float:
    """Share of a finished mask's own pixels that are close enough to the surrounding
    backdrop colour to plausibly be backdrop that leaked in, rather than genuine element.

    Deliberately the same test `_strip_gain` runs on one growth strip, just over the whole
    mask: a mask that bled in a neighbour did not necessarily grow to do it (GoldCoinIcon's
    did not), so this cannot be folded into the growth loop and has to stand on its own as
    part of what `extract_region` checks every finished mask against."""
    if backdrop_rgb is None or not mask.any():
        return 0.0
    pixels = crop_rgb[mask].astype(np.float32)
    dist = np.linalg.norm(pixels - np.asarray(backdrop_rgb, np.float32), axis=1)
    return float((dist < _GROW_BG_TOL).mean())


def _foreign_blob_frac(mask: np.ndarray, crop_rgb: np.ndarray) -> float:
    """Largest single contiguous chunk of the mask that (a) doesn't match the mask's own
    dominant colour and (b) touches the box border, as a fraction of the mask's area.

    `_backdrop_bleed` only catches a neighbour that happens to be coloured like the sampled
    backdrop ring — true for a coin bleeding into the pill it sits on, useless for a badge
    that ate a fragment of a tower spire behind it, since the spire is neither the badge's
    colour nor the sky's. This catches that case without needing to know the intruder's
    colour in advance: a neighbouring object riding along on the mask arrives as ONE solid
    piece glued to the box's edge, where the element's own internal detail — lettering, an
    icon's shading, a highlight — is real colour variance too, but never assembles into a
    single blob pinned to the border.

    Border-touching alone does NOT tell the two apart, which is what this originally
    claimed. An element's own outline is off-colour by definition, is one connected
    component because it is a ring, and touches the border on all four sides because a tight
    box is drawn flush to it — a gold-keylined blue panel, the single most common shape in
    this UI, scored 0.18 and sent every such button through the shrink ladder. `_is_shell`
    is the missing half of the test.
    """
    import cv2

    if not mask.any():
        return 0.0
    dominant = np.median(crop_rgb[mask].astype(np.float32), axis=0)
    off = (np.linalg.norm(crop_rgb.astype(np.float32) - dominant, axis=2) > _GROW_BG_TOL) & mask
    if not off.any():
        return 0.0
    n, labels, stats, _ = cv2.connectedComponentsWithStats(off.astype(np.uint8), 8)
    if n <= 1:
        return 0.0
    ys, xs = np.where(mask)
    mask_bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    total = float(mask.sum())
    best = 0.0
    for i in range(1, n):
        comp = labels == i
        touches = comp[0, :].any() or comp[-1, :].any() or comp[:, 0].any() or comp[:, -1].any()
        if touches and not _is_shell(stats[i], mask_bbox):
            best = max(best, stats[i, cv2.CC_STAT_AREA] / total)
    return best


def _is_shell(comp_stats, mask_bbox: tuple[int, int, int, int]) -> bool:
    """Whether a connected component is the element's own outline rather than an intruder.

    A shell wraps the mask: it reaches across the mask's full extent on both axes while
    enclosing almost nothing, because it is one pixel-wide-ish ring around the outside. An
    intruder is the opposite on both counts — a compact region covering part of the mask,
    filled in solidly. Requiring both conditions is what keeps a genuinely large neighbour
    from hiding behind the first one.
    """
    import cv2

    x = int(comp_stats[cv2.CC_STAT_LEFT])
    y = int(comp_stats[cv2.CC_STAT_TOP])
    w = int(comp_stats[cv2.CC_STAT_WIDTH])
    h = int(comp_stats[cv2.CC_STAT_HEIGHT])
    mx0, my0, mx1, my1 = mask_bbox
    span_x = w / max(1, mx1 - mx0 + 1)
    span_y = h / max(1, my1 - my0 + 1)
    fill = int(comp_stats[cv2.CC_STAT_AREA]) / max(1, w * h)
    return span_x >= _SHELL_SPAN and span_y >= _SHELL_SPAN and fill < _SHELL_FILL


def _mask_bleed(
    mask: np.ndarray, crop_rgb: np.ndarray, backdrop_rgb: np.ndarray | None
) -> float:
    """Combined self-assessment score for a finished mask — the worse of the two ways a
    neighbour rides in uninvited. See `_backdrop_bleed` and `_foreign_blob_frac`."""
    return max(
        _backdrop_bleed(mask, crop_rgb, backdrop_rgb),
        _foreign_blob_frac(mask, crop_rgb),
    )


def _pick_candidate(candidates: list[tuple]):
    """The retry ladder's winner, from `(bleed, box, found, tightened)` rows.

    Lowest bleed wins — including the original, so a retry can never leave the result worse
    than what growth already produced — with one exception: a `tightened` candidate, one
    whose box cuts INSIDE the box that was actually asked for, must also land under
    `_BLEED_RETRY_FRAC`. It has to SOLVE the bleed, not merely score lower.

    Pure ranking was the bug. Ranking silently assumes the score is measuring an intruder,
    so the lower of two bad scores must be the less-invaded mask. `_foreign_blob_frac` on a
    large composite element measures the element's own artwork instead — and then "lowest
    score" just means "smallest box", because a smaller box holds less artwork. The
    Matchmaking versus panel (a gold frame around two character portraits) scored 0.84 at
    the rect the user drew and 0.76 one rung tighter, so the tighter box won every single
    build and the panel came back clipped 10% on all four sides; widening the region only
    fed a bigger box to the same 10% cut. Neither candidate was anywhere near clean, which
    is the tell — nothing had been eaten, the diagnosis was wrong, and the crop was pure
    loss. The wins this ladder exists for (GoldCoinIcon, EventsNavIcon) come back clean and
    are unaffected.

    Note this is deliberately not a "did it improve" test. Improvement is what ranking
    already measures, and measuring it twice would not have caught this.
    """
    usable = [c for c in candidates if not c[3] or c[0] <= _BLEED_RETRY_FRAC] or candidates
    return min(usable, key=lambda c: c[0])


def _shrink(box, frac: float, W: int, H: int, scale: int = 1):
    """Pull all four sides of `box` in by `frac` of its own span, floored so a very small
    box still moves. The inverse of `_grow`, but a single fixed step rather than iterative —
    this is the retry ladder's last rung, not a search."""
    x0, y0, x1, y1 = box
    floor = _GROW_MIN_PX * max(1, int(scale))
    dx = max(floor, int((x1 - x0) * frac / 2))
    dy = max(floor, int((y1 - y0) * frac / 2))
    nx0, ny0, nx1, ny1 = x0 + dx, y0 + dy, x1 - dx, y1 - dy
    if nx1 <= nx0 or ny1 <= ny0:
        return box
    return (max(0, nx0), max(0, ny0), min(W, nx1), min(H, ny1))


def _grow(box, sides, budget, W, H, scale: int = 1):
    """Push `sides` of `box` outward by one step, honouring the per-side growth budget
    and the image bounds. Returns the new box (unchanged if nothing could move)."""
    x0, y0, x1, y1 = box
    floor = _GROW_MIN_PX * max(1, int(scale))
    step_x = max(floor, int((x1 - x0) * _GROW_STEP))
    step_y = max(floor, int((y1 - y0) * _GROW_STEP))
    for side in sides:
        take = min(step_x if side in ("left", "right") else step_y, budget[side])
        if take <= 0:
            continue
        budget[side] -= take
        if side == "left":
            x0 = max(0, x0 - take)
        elif side == "right":
            x1 = min(W, x1 + take)
        elif side == "top":
            y0 = max(0, y0 - take)
        else:
            y1 = min(H, y1 + take)
    return (x0, y0, x1, y1)


def _side_slice(shape, old_box, new_box, side: str):
    """Index expression selecting the strip added on `side` of a grown box."""
    ox0, oy0, ox1, oy1 = old_box
    nx0, ny0, _, _ = new_box
    ix0, iy0 = ox0 - nx0, oy0 - ny0                       # old box inside the new crop
    ix1, iy1 = ix0 + (ox1 - ox0), iy0 + (oy1 - oy0)
    return {
        "left": (slice(iy0, iy1), slice(0, ix0)),
        "right": (slice(iy0, iy1), slice(ix1, shape[1])),
        "top": (slice(0, iy0), slice(ix0, ix1)),
        "bottom": (slice(iy1, shape[0]), slice(ix0, ix1)),
    }[side]


def _strip_gain(
    mask: np.ndarray, crop_rgb: np.ndarray, old_box, new_box, side: str,
    bg_rgb: np.ndarray | None,
) -> float:
    """Fraction of the strip added on `side` that came back as element *and* does not look
    like the backdrop.

    This is the whole test for whether a box was truncating something. Asking "does the
    mask touch the border?" cannot distinguish a clipped element from a correctly tight
    box — both touch. Growing the box and re-segmenting can: if the element really did
    continue, the segmenter finds it in the new strip; if the box was already right, the
    strip is backdrop and the growth is reverted.

    The colour half of the test is not belt-and-braces, it is what makes the test work.
    A segmenter reseeded on the grown box re-labels a slice of the new strip as element
    whatever is actually there — on a correctly-boxed panel that leakage measured 14% of
    the strip, comfortably past any coverage threshold loose enough to catch a sword tip
    poking three pixels out of its box. Leaked pixels are backdrop-coloured and recovered
    ones are not, which separates the two cleanly at any threshold instead of forcing a
    trade between amputating tips and inflating boxes.
    """
    idx = _side_slice(mask.shape, old_box, new_box, side)
    strip = mask[idx]
    if not strip.size:
        return 0.0
    if bg_rgb is not None:
        pixels = crop_rgb[idx].astype(np.float32)
        strip = strip & (np.linalg.norm(pixels - np.asarray(bg_rgb, np.float32), axis=2)
                         > _GROW_BG_TOL)
    return float(strip.mean())


def _retained(
    old_mask: np.ndarray, new_mask: np.ndarray, old_box, new_box
) -> float:
    """Fraction of the pre-growth mask still selected after the box moved.

    Growth exists to recover an element the box was cutting in half, so by construction it
    must *extend* what was already found. A re-segmentation that finds something else
    entirely has not recovered anything — it has changed subject, and `_strip_gain` cannot
    tell the difference because the new subject is not backdrop-coloured either.

    That is not hypothetical. The player avatar sits against a mountain: SAM2 returned the
    avatar at 63.8% of the box, growth pushed the left edge out to the screen border, SAM2
    then returned the MOUNTAIN at 12.9%, the new strip was full of non-backdrop pixels so
    the gain test passed, and the exported sprite was a mountain with no avatar in it.
    Requiring the old selection to survive rejects that in one line.
    """
    ox0, oy0, ox1, oy1 = old_box
    nx0, ny0, _, _ = new_box
    iy0, ix0 = oy0 - ny0, ox0 - nx0
    window = new_mask[iy0:iy0 + (oy1 - oy0), ix0:ix0 + (ox1 - ox0)]
    if window.shape != old_mask.shape or not old_mask.any():
        return 0.0
    return float((window & old_mask).sum()) / float(old_mask.sum())


def extract_region(
    screenshot: Image.Image,
    box_pct: tuple[float, float, float, float],
    method: str = "auto",
    feather: bool = True,
    grow: bool = True,
    source: tuple[np.ndarray, int] | None = None,
    colour: np.ndarray | None = None,
    shadow_margin: bool = False,
) -> tuple[Image.Image, str, tuple[float, float, float, float]]:
    """Cut the element at `box_pct` (x, y, w, h as 0-100 percentages) out of `screenshot`.

    Returns (RGBA image, backend_used, final_box_pct). `backend_used` is one of
    "sam2", "classical" or "box" — the last meaning every segmenter failed and the raw
    rectangle was returned opaque, which is still a better asset than a hallucinated
    redraw.

    `source` is an optional `(rgb_array, scale)` pair from `upres.supersample_cached`,
    letting the caller hand in an already-enlarged screenshot. The returned sprite is then
    `scale` times the box in pixels while `final_box_pct` stays in percent, so everything
    downstream — the compositor, the Unity layout, the region rect write-back — is
    unaffected, and `fit_to_resolution` ends up *reducing* the sprite to its export target
    instead of enlarging it. Left None, extraction runs at the screenshot's own resolution.

    `colour` is where the sprite's PIXELS come from, defaulting to `source`. Splitting the
    two is the difference between using super-resolution and being used by it. A generative
    upscaler earns its place at the segmentation step — SAM2 localises a boundary far better
    with sixteen times the pixels, and that is what restored the dark keyline around every
    icon. But it *invents* what it adds, and measured as a round trip (x4 then back down)
    Real-ESRGAN shifts colour by ΔE 2.57 where plain cubic shifts it by 0.38. Since the
    entire premise of extracting rather than generating is that the screenshot's own pixels
    are the ground truth, handing the colour channel to a hallucinating model gives that
    away at the last step. So: find the edge with the model, fill it from the photograph.

    `final_box_pct` may be LARGER than the box passed in. A detector box that clips its
    element — the raised arch of a nav bar's active tab, a gem overhanging its pill — used
    to produce a sprite with a slice missing, and no amount of prompt wording reliably
    prevents it. When `grow` is set, a mask that runs into the border is treated as
    evidence the element continues past it: the box is pushed out and re-segmented, and
    the growth is kept only if the segmenter actually finds element in the new strip.
    Callers should write the returned rect back to the region so the sprite is composited
    where it was really cut from.

    `shadow_margin` extends `final_box_pct` a bit further still, past whatever `grow`
    settled on, specifically to give a soft cast shadow or an outer keyline stroke — real
    screenshot pixels that SAM2's binary mask never claims because they're not the object's
    own opaque fill — somewhere to be recovered from. See `_widen_for_shadow` and
    `_shadow_trimap`. Off by default: it is a real extra margin on the returned rect, right
    for a frame-like element with room around it, wrong for a small icon packed against its
    neighbours.
    """
    import cv2

    if source is not None:
        rgb_full, scale = source
        rgb_full = np.asarray(rgb_full)
    else:
        rgb_full, scale = np.asarray(screenshot.convert("RGB")), 1
    scale = max(1, int(scale))
    H, W = rgb_full.shape[:2]
    paint_full = rgb_full if colour is None else np.asarray(colour)
    if paint_full.shape[:2] != (H, W):
        paint_full = cv2.resize(paint_full, (W, H), interpolation=cv2.INTER_CUBIC)

    x, y, w, h = box_pct
    x0 = max(0, min(W - 1, int(round(W * x / 100))))
    y0 = max(0, min(H - 1, int(round(H * y / 100))))
    x1 = max(x0 + 1, min(W, int(round(W * (x + w) / 100))))
    y1 = max(y0 + 1, min(H, int(round(H * (y + h) / 100))))

    full_bgr = cv2.cvtColor(rgb_full, cv2.COLOR_RGB2BGR)
    box = (x0, y0, x1, y1)
    detect_box = box  # the box as detected, before growth — the retry ladder's anchor
    band = _TRIMAP_BAND * scale

    def run(backend: str, bx) -> np.ndarray | None:
        """A cleaned mask from one backend, or None if it failed or is unusable.
        Any backend failure (missing model, OOM, cv2 error) is recoverable."""
        shape = (bx[3] - bx[1], bx[2] - bx[0])
        try:
            raw = (classical_mask(full_bgr, bx, scale) if backend == "classical"
                   else sam2_mask(rgb_full, bx))
        except Exception:
            return None
        if raw is None or raw.shape != shape or not raw.any():
            return None
        cleaned = _clean_mask(raw, scale)
        return None if _mask_is_degenerate(cleaned) else cleaned

    def best(bx) -> tuple[np.ndarray, str] | None:
        """SAM2, or GrabCut when SAM2 can't answer.

        Strictly ordered, with no comparison between them, because the comparison was the
        bug. Ranking two masks by how much *element* they captured is what let GrabCut's
        sword-plus-blue-slab beat SAM2's sword, since the slab counts as element on any
        area-based measure; and every colour-based correction bolted on afterwards to
        strip that slab back off (`_reject_backdrop`, `_recover_notches`) either failed to
        remove it or ate the middle out of an icon whose interior legitimately matched the
        frame it sat on. SAM2 simply does not make the mistake, so the right fix was to ask
        it first and delete the corrections.
        """
        if method in ("classical", "sam2"):
            m = run(method, bx)
            return (m, method) if m is not None else None
        sam = run("sam2", bx)
        if sam is not None:
            return sam, "sam2"
        classical = run("classical", bx)
        return (classical, "classical") if classical is not None else None

    found = best(box)

    if grow and found is not None:
        # Sampled once, from the box as detected: after a couple of growth rounds the ring
        # outside the box has moved and would answer with the element's own surroundings.
        backdrop = _backdrop_sample(rgb_full, box, scale)
        budget = {
            "left": int((x1 - x0) * _GROW_MAX), "right": int((x1 - x0) * _GROW_MAX),
            "top": int((y1 - y0) * _GROW_MAX), "bottom": int((y1 - y0) * _GROW_MAX),
        }
        for side in ("left", "right", "top", "bottom"):
            for _ in range(_GROW_ROUNDS):
                if side not in _clipped_sides(found[0]):
                    break
                trial_box = _grow(box, [side], budget, W, H, scale)
                if trial_box == box:
                    break
                trial = best(trial_box)
                if trial is None:
                    break
                if _retained(found[0], trial[0], box, trial_box) < _GROW_KEEP_FRAC:
                    break   # it found a different object, not more of this one
                tx0, ty0, tx1, ty1 = trial_box
                gain = _strip_gain(
                    trial[0], rgb_full[ty0:ty1, tx0:tx1], box, trial_box, side, backdrop
                )
                if gain < _GROW_MIN_GAIN:
                    break
                cx0, cy0, cx1, cy1 = box
                current_bleed = _mask_bleed(found[0], paint_full[cy0:cy1, cx0:cx1], backdrop)
                trial_bleed = _mask_bleed(trial[0], paint_full[ty0:ty1, tx0:tx1], backdrop)
                if trial_bleed > current_bleed + _BLEED_GROW_TOL:
                    break   # the strip alone looked like a gain; the mask as a whole did not
                box, found = trial_box, trial

    if found is not None:
        # Self-assessment: does the finished mask still look like it ate its surroundings?
        # Judged the same way growth judges one strip — colour distance from that box's own
        # sampled backdrop — just over the whole mask, and run unconditionally rather than
        # only after growth, because the failure isn't specific to growth.
        bx0, by0, bx1, by1 = box
        candidates = [(
            _mask_bleed(found[0], paint_full[by0:by1, bx0:bx1], _backdrop_sample(rgb_full, box, scale)),
            box, found, False,
        )]
        if candidates[0][0] > _BLEED_RETRY_FRAC:
            # (box, goes_inside_detect_box). The first rung only undoes growth, so it can
            # never return less than the box that was asked for. The second cuts INTO that
            # box, and `_pick_candidate` makes it earn that.
            trials = [(detect_box, False)] if box != detect_box else []
            trials.append((_shrink(detect_box, _BLEED_SHRINK_STEP, W, H, scale), True))
            for trial_box, tightened in trials:
                if trial_box == box:
                    continue
                trial = best(trial_box)
                if trial is None:
                    continue
                ttx0, tty0, ttx1, tty1 = trial_box
                bleed = _mask_bleed(
                    trial[0], paint_full[tty0:tty1, ttx0:ttx1],
                    _backdrop_sample(rgb_full, trial_box, scale),
                )
                candidates.append((bleed, trial_box, trial, tightened))
            _, box, found, _ = _pick_candidate(candidates)

    x0, y0, x1, y1 = box
    crop_rgb = paint_full[y0:y1, x0:x1]
    final_pct = (100.0 * x0 / W, 100.0 * y0 / H, 100.0 * (x1 - x0) / W, 100.0 * (y1 - y0) / H)

    if found is not None:
        mask, name = found
        if shadow_margin:
            s_box, s_mask, s_margin = _widen_for_shadow(box, mask, W, H, scale)
            sx0, sy0, sx1, sy1 = s_box
            s_crop = paint_full[sy0:sy1, sx0:sx1]
            alpha, rgb = _solve_matte(s_crop, _shadow_trimap(s_mask, band, s_margin))
            out = np.dstack([rgb, np.rint(np.clip(alpha, 0, 1) * 255).astype(np.uint8)])
            shadow_pct = (
                100.0 * sx0 / W, 100.0 * sy0 / H, 100.0 * (sx1 - sx0) / W, 100.0 * (sy1 - sy0) / H,
            )
            return Image.fromarray(out, "RGBA"), name, shadow_pct
        return _to_rgba(crop_rgb, mask, feather=feather, band=band), name, final_pct

    # Nothing segmented cleanly. The crop's own pixels are still the reference truth, so
    # return them whole rather than nothing.
    opaque = np.dstack([crop_rgb, np.full(crop_rgb.shape[:2], 255, np.uint8)])
    return Image.fromarray(opaque, "RGBA"), "box", final_pct
