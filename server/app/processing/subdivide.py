"""Find the seams inside one detected element, so it can be split into sub-assets.

A screen element is very often a container: a capsule holding a gem, a nav bar holding
five icons, one box the detector drew around two stacked currency pills. Extracting such
a box whole bakes the foreground into the background — the frame can never be restyled,
recoloured or reused, and the icon can never be moved. Splitting it first is what makes
both halves reusable.

This module is the cheap, deterministic half of that: it decides which elements are worth
asking about and where the repeated bands are. The semantic half — what each piece *is*,
and what to call it — comes from a vision pass in `routers/mockups.py`, because naming a
"faceted purple gem" is not something a gradient profile can do.

Nothing here mutates anything; every function answers a question about pixels or rects.
"""

import cv2
import numpy as np
from PIL import Image

from .region_detector import compute_gradient_map, containment_ratio, hamming, perceptual_hash

# A band has to be this fraction of the crop before it counts as a repeat unit rather
# than a border highlight or a seam. Two currency pills stacked in one box are ~45% each.
MIN_BAND_FRAC = 0.15
# A separator run must be at least this fraction of the crop to be a real gap between
# two instances, rather than the one-pixel trough between a bevel and its highlight.
MIN_GAP_FRAC = 0.03
# Rows/columns quieter than this multiple of the crop's median energy are "empty".
QUIET_RATIO = 0.35
# Repeat units must hash this close to count as the same artwork. Matches the
# DUP_HASH_THRESHOLD used for cross-region dedup in routers/mockups.py.
REPEAT_HASH_THRESHOLD = 8
# ...and their colours must agree to this many levels per channel. The hash alone is not
# enough: `perceptual_hash` reads luminance, so four same-shaped buttons in four different
# colours hash identically, and reporting those as "four copies of one thing" would bind
# them all to a single asset and throw the colours away. Compared on the MEDIAN colour
# rather than the mean, because the median is the frame's own fill — a small icon in the
# corner cannot drag it, which is what keeps a gem pill and a gold pill reading as the
# same component (they are; only their icons differ).
REPEAT_COLOUR_TOLERANCE = 20.0
# Below this the element is too small for a split to be worth proposing — the pieces
# would each be a handful of pixels.
MIN_SPLITTABLE_PX = 48


def _energy_profile(crop: Image.Image, axis: int) -> "np.ndarray":
    """Per-row (axis=0) or per-column (axis=1) painted-content energy.

    Gradient magnitude rather than raw colour: a pill's interior and the screen behind it
    can be a similar brightness, but the *edges* of a pill are unmistakable, and what we
    are looking for is the quiet strip between two elements.
    """
    bgr = cv2.cvtColor(np.asarray(crop.convert("RGB")), cv2.COLOR_RGB2BGR)
    grad = compute_gradient_map(bgr)
    return grad.mean(axis=1 if axis == 0 else 0)


def _bands(profile: "np.ndarray", length: int) -> list[tuple[int, int]]:
    """Split `profile` into content bands separated by quiet runs.

    Returns [(start, end), ...] in profile index space. Leading and trailing quiet runs
    are trimmed rather than treated as separators — they are the element's own margin.
    """
    if length < 2 * MIN_SPLITTABLE_PX:
        return []
    median = float(np.median(profile))
    if median <= 0:
        return []
    quiet = profile < (median * QUIET_RATIO)

    min_gap = max(2, int(round(length * MIN_GAP_FRAC)))
    bands: list[tuple[int, int]] = []
    start = None
    run = 0
    for i in range(length):
        if quiet[i]:
            run += 1
            continue
        if run >= min_gap and start is not None:
            bands.append((start, i - run))
            start = None
        run = 0
        if start is None:
            start = i
    if start is not None:
        bands.append((start, length - run if run else length))

    min_band = max(MIN_SPLITTABLE_PX // 2, int(round(length * MIN_BAND_FRAC)))
    return [(a, b) for a, b in bands if b - a >= min_band]


def _median_colour(img: Image.Image) -> "np.ndarray":
    """The tile's dominant colour, as a per-channel median."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float32).reshape(-1, 3)
    return np.median(arr, axis=0)


def repeat_units(crop: Image.Image) -> tuple[str, list[tuple[int, int, int, int]]] | None:
    """Detect that this crop is really N copies of the same thing side by side.

    This is the "one box drawn around both currency pills" case. Returns
    (axis, [(x, y, w, h), ...]) in crop pixels, or None. Bands only count as a repeat when
    they hash alike — five *different* nav icons in a row are a container, not a repeat,
    and want a different question asked about them.
    """
    w, h = crop.size
    if w < MIN_SPLITTABLE_PX or h < MIN_SPLITTABLE_PX:
        return None

    for axis, length in ((0, h), (1, w)):
        bands = _bands(_energy_profile(crop, axis), length)
        if len(bands) < 2:
            continue
        rects = [
            (0, a, w, b - a) if axis == 0 else (a, 0, b - a, h)
            for a, b in bands
        ]
        tiles = [crop.crop((x, y, x + bw, y + bh)) for x, y, bw, bh in rects]
        hashes = [perceptual_hash(t) for t in tiles]
        if any(hv is None for hv in hashes):
            continue  # a flat tile has no fingerprint — refuse rather than guess
        if not all(hamming(hashes[0], hv) <= REPEAT_HASH_THRESHOLD for hv in hashes[1:]):
            continue
        colours = [_median_colour(t) for t in tiles]
        if not all(
            float(np.max(np.abs(colours[0] - c))) <= REPEAT_COLOUR_TOLERANCE
            for c in colours[1:]
        ):
            continue
        return ("vertical" if axis == 0 else "horizontal", rects)
    return None


def to_screen_pct(
    parent: tuple[float, float, float, float], cx: float, cy: float, cw: float, ch: float
) -> tuple[float, float, float, float]:
    """Map a rect given in percentages *of the parent crop* into screen percentages."""
    px, py, pw, ph = parent
    x = px + (cx / 100.0) * pw
    y = py + (cy / 100.0) * ph
    w = (cw / 100.0) * pw
    h = (ch / 100.0) * ph
    x = max(0.0, min(99.5, round(x, 2)))
    y = max(0.0, min(99.5, round(y, 2)))
    return x, y, max(0.3, min(100.0 - x, round(w, 2))), max(0.3, min(100.0 - y, round(h, 2)))


# A proposed child this close to a box that already exists is that box — don't offer to
# create a duplicate of a region the user already has.
DUPLICATE_OVERLAP = 0.7


def is_already_covered(
    child: tuple[float, float, float, float], siblings: list[tuple[float, float, float, float]]
) -> bool:
    """Whether a proposed child rect is already represented by an existing region."""
    return any(containment_ratio(child, s) >= DUPLICATE_OVERLAP for s in siblings)


def has_children(
    parent: tuple[float, float, float, float], others: list[tuple[float, float, float, float]]
) -> bool:
    """Whether any existing region already sits inside `parent`.

    A parent that already has its icons boxed separately has been decomposed — by the
    detector or by hand — and proposing to split it again would only produce duplicates.
    """
    parea = parent[2] * parent[3]
    for other in others:
        if other[2] * other[3] >= 0.85 * parea:
            continue
        if containment_ratio(other, parent) >= DUPLICATE_OVERLAP:
            return True
    return False
