"""Auto-detect 9-slice borders.

A 9-slice border marks off the parts of a sprite that must NOT stretch. The stretchable
middle is, by definition, a run of lines that are all the same — stretching it just
repeats what is already there, so nothing visibly deforms. That is the primary signal
this detector uses: **find the longest run of near-identical adjacent rows (and columns);
everything outside it is border.** (Same principle as Auto9Slicer for Unity.)

Two constraints refine it, each taken as a *minimum* border depth:

1. **Corner geometry (alpha silhouette).** The border must be at least as deep as the
   rounded/decorated corner, or the curve spills into the stretchable edge and gets
   pinched into a notch when scaled.
2. **Shading bands (pixel colours).** Buttons are often 3D-shaded: a light cap, a darker
   body, a bottom lip. Used only as a fallback when the image is a gradient with no truly
   uniform run, because on its own it mistakes the cap/body seam for the border — the bug
   that produced `PlayButton` borders of `[75, 232, 74, 60]` on a 468px-tall sprite, where
   the "bottom border" was half the image and the stretchable band was 49px.

Everything is returned in the pixel space of the passed-in image so it matches the
exported PNG, with transparent padding folded into the border.
"""
import numpy as np
from PIL import Image

_ALPHA = 16          # opaque cutoff
_BAND_DELTA = 8.0    # mean per-channel colour change that counts as a candidate band transition
_BAND_WINDOW_FRAC = 1 / 12  # local baseline window (as a fraction of the axis span) a
                            # candidate transition must stand out against, so a sustained
                            # gradient ramp isn't chopped into spurious bands
_STRIP_FRACS = (0.5, 0.33, 0.2)  # widths (fraction of the perpendicular axis) of the
                                  # centered strips sampled per axis; the median across
                                  # them keeps one strip crossing a stray decoration from
                                  # skewing the result
_SYMMETRY_TOL = 0.08  # snap a border pair to their max when they differ by less than
                      # this fraction of the span (most UI frames are symmetric; this
                      # absorbs small detection noise between the two sides)

# Mean absolute per-channel difference below which two adjacent lines count as "the same"
# for uniform-run detection. Generous enough to tolerate JPEG noise and a slow gradient,
# tight enough that a real seam breaks the run.
_UNIFORM_TOL = 3.0
# A uniform run must be at least this fraction of the axis to be believed as the
# stretchable middle; shorter than this and we're looking at a flat detail, not the body.
_MIN_RUN_FRAC = 0.10
# No single border may exceed this fraction of the span unless the opposite side agrees
# it is that deep too. A border past this point means the detector locked onto interior
# content instead of the frame.
_MAX_BORDER_FRAC = 0.40
# ...and what the two borders leave between them has to be a band worth stretching.
#
# This is the check the per-side cap cannot make, and its absence was the single largest
# drag on reconstruction fidelity. Two "symmetric" borders of 39% each pass every per-side
# test and leave 22% of the sprite stretchable; measured on the rebuilt ClashUp lobby the
# nav bar came back l=348 r=348 on an 871px sprite (80% border, and both sides just under
# the 0.40 cap, so `_reject_runaway` saw nothing wrong and the symmetry test read a
# perfect 1.0). Sliced on those borders it scored 82 against 90 stretched; the currency
# pill 46 against 89, the level badge 42 against 80, the GAME MODE button 54 against 91.
#
# A quarter of the span is the point below which "9-slice frame" stops describing the
# sprite. Anything tighter is a picture, and a picture should stretch as one.
_MIN_CENTER_FRAC = 0.25


def _corner_insets(profile: np.ndarray, span: int) -> tuple[int, int]:
    """profile = opaque-pixel count per line. Returns (leading, trailing) distance
    until the shape reaches (near) its full extent — the corner radius on each end."""
    if span <= 2 or profile.size == 0:
        return 0, 0
    peak = int(profile.max())
    if peak <= 0:
        return 0, 0
    thresh = peak - max(1, round(0.005 * peak))  # reach the true plateau; tolerate only AA
    reached = profile >= thresh
    lead = int(np.argmax(reached))
    trail = int(np.argmax(reached[::-1]))
    return lead, trail


def _uniform_run(lines: np.ndarray) -> tuple[int, int] | None:
    """Longest run of near-identical adjacent lines in `lines` (span x channels).

    Returns (start, end) as a half-open index range, or None when no run is long enough
    to be the stretchable middle. This is the core of the detector: a region that repeats
    is a region that survives stretching.
    """
    span = lines.shape[0]
    if span < 4:
        return None
    # diff[i] compares line i to line i+1
    diff = np.abs(np.diff(lines.astype(np.float32), axis=0)).mean(axis=1)
    same = diff <= _UNIFORM_TOL

    best_len, best_start = 0, None
    run_start = None
    for i, flag in enumerate(same):
        if flag:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and (i - run_start) > best_len:
                best_len, best_start = i - run_start, run_start
            run_start = None
    if run_start is not None and (len(same) - run_start) > best_len:
        best_len, best_start = len(same) - run_start, run_start

    if best_start is None or best_len < max(2, int(span * _MIN_RUN_FRAC)):
        return None
    # `best_len` counts diffs; the run spans one more line than that.
    return best_start, best_start + best_len + 1


def _band_transitions(delta: np.ndarray) -> list[int]:
    """Pick out sharp, isolated colour jumps (real seams) while ignoring sustained runs
    of similarly elevated delta (a gradient ramp). A candidate row only counts if it
    clears _BAND_DELTA *and* stands out against the median of its local neighbourhood —
    a ramp's rows all look like their neighbours, so none of them clear that bar."""
    n = delta.size
    if n == 0:
        return []
    window = max(3, round(n * _BAND_WINDOW_FRAC))
    accepted = []
    for i in range(n):
        if delta[i] <= _BAND_DELTA:
            continue
        lo, hi = max(0, i - window), min(n, i + window + 1)
        baseline = np.delete(delta[lo:hi], i - lo)
        base_level = float(np.median(baseline)) if baseline.size else 0.0
        if delta[i] < max(_BAND_DELTA, 2.0 * base_level):
            continue
        accepted.append(i)
    if not accepted:
        return []
    # collapse runs of adjacent accepted rows (anti-aliasing spanning >1 row of one seam)
    # into the single strongest row
    groups: list[list[int]] = [[accepted[0]]]
    for i in accepted[1:]:
        if i - groups[-1][-1] <= 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [max(g, key=lambda i: delta[i]) + 1 for g in groups]


def _band_insets(strip: np.ndarray, span: int) -> tuple[int, int]:
    """strip = mean colour per line (span x 3) taken over the centre of the other axis.
    Splits the axis into flat bands at sharp colour transitions and returns the insets
    that keep every band except the largest central one fixed. (0, 0) if there is only
    one band (nothing internal to preserve)."""
    if span <= 2 or strip.shape[0] != span:
        return 0, 0
    delta = np.abs(np.diff(strip, axis=0)).mean(axis=1)
    trans = _band_transitions(delta)
    if not trans:
        return 0, 0
    bounds = [0, *trans, span]
    bands = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]  # [start, end)
    centre = span // 2
    # prefer the flat band the geometric centre falls in, but only if it's wide enough to
    # trust; a sliver band at the centre is more likely noise than a genuine seam, so fall
    # back to the widest band instead
    at_centre = next((b for b in bands if b[0] <= centre < b[1]), None)
    if at_centre is not None and (at_centre[1] - at_centre[0]) >= 0.15 * span:
        central = at_centre
    else:
        central = max(bands, key=lambda b: b[1] - b[0])
    a, b = central
    return a, span - b  # leading inset = band start, trailing inset = span - band end


def _median_band_insets(strips: list[np.ndarray], span: int) -> tuple[int, int]:
    """Run _band_insets over several independently-sampled strips and take the median
    inset per side, so one strip crossing a stray decoration can't skew the result."""
    results = [_band_insets(s, span) for s in strips]
    if not results:
        return 0, 0
    return (
        int(np.median([r[0] for r in results])),
        int(np.median([r[1] for r in results])),
    )


def _symmetry_snap(a: int, b: int, span: int) -> tuple[int, int]:
    """Most UI frames are symmetric; if the two sides are already close, snap them to
    their max to absorb small per-side detection noise."""
    if span <= 0 or (a == 0 and b == 0):
        return a, b
    if abs(a - b) <= max(1, _SYMMETRY_TOL * span):
        m = max(a, b)
        return m, m
    return a, b


def _reject_runaway(lead: int, trail: int, span: int) -> tuple[int, int]:
    """Cap a border that ran away into the sprite's interior.

    A frame's border is a frame; when one side claims a third of the sprite while the
    other claims a tenth, the deep side locked onto interior content (a gloss seam, a
    baked-in glyph) rather than the frame. Fall that side back to its partner instead of
    exporting a 9-slice whose stretchable band is a sliver."""
    limit = _MAX_BORDER_FRAC * span
    if lead > limit and trail <= limit:
        lead = max(trail, int(limit))
    elif trail > limit and lead <= limit:
        trail = max(lead, int(limit))
    return lead, trail


def detect_borders(img: Image.Image) -> dict:
    """Returns {l, t, r, b} border sizes in pixels of the full (passed-in) image."""
    borders, _ = _detect(img)
    return borders


def _detect(img: Image.Image) -> tuple[dict, dict]:
    """Borders plus evidence about how they were derived.

    The evidence matters: borders found from a real uniform run describe an actual
    stretchable band, while borders from the shading-band fallback are a guess about a
    gradient. Only the former justifies 9-slicing — see `is_sliceable`."""
    rgb = np.asarray(img.convert("RGB")).astype(np.int16)
    rgba = np.asarray(img.convert("RGBA"))
    h, w = rgba.shape[:2]
    opaque = rgba[..., 3] > _ALPHA

    ys, xs = np.where(opaque)
    if len(xs) == 0:
        return ({"l": max(1, w // 3), "t": max(1, h // 3),
                 "r": max(1, w // 3), "b": max(1, h // 3)},
                {"v_run": False, "h_run": False})

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    content = opaque[y0:y1 + 1, x0:x1 + 1]
    bw, bh = x1 - x0 + 1, y1 - y0 + 1

    # 1. corner radius from the silhouette — a floor on every side
    top_rad, bot_rad = _corner_insets(content.sum(axis=1), bh)
    left_rad, right_rad = _corner_insets(content.sum(axis=0), bw)

    # 2. PRIMARY: the longest run of near-identical lines is the stretchable middle.
    # Include alpha so a run of fully-transparent lines can't masquerade as uniform body.
    content_rgba = rgba[y0:y1 + 1, x0:x1 + 1, :].astype(np.float32)
    rows = content_rgba.mean(axis=1)   # (bh, 4) — one mean colour per row
    cols = content_rgba.mean(axis=0)   # (bw, 4) — one mean colour per column

    v_run = _uniform_run(rows)
    h_run = _uniform_run(cols)

    if v_run is not None:
        top_i, bot_i = v_run[0], bh - v_run[1]
    else:
        # 3. FALLBACK: no uniform run (a fully gradient body) — fall back to shading bands.
        content_rgb = rgb[y0:y1 + 1, x0:x1 + 1, :]
        row_strips = []
        for frac in _STRIP_FRACS:
            half_w = max(1, int(bw * frac / 2))
            cx = bw // 2
            mx0, mx1 = max(0, cx - half_w), min(bw, cx + half_w)
            if mx1 > mx0:
                row_strips.append(content_rgb[:, mx0:mx1, :].mean(axis=1))
        top_i, bot_i = _median_band_insets(row_strips, bh)

    if h_run is not None:
        left_i, right_i = h_run[0], bw - h_run[1]
    else:
        content_rgb = rgb[y0:y1 + 1, x0:x1 + 1, :]
        col_strips = []
        for frac in _STRIP_FRACS:
            half_h = max(1, int(bh * frac / 2))
            cy = bh // 2
            my0, my1 = max(0, cy - half_h), min(bh, cy + half_h)
            if my1 > my0:
                col_strips.append(content_rgb[my0:my1, :, :].mean(axis=0))
        left_i, right_i = _median_band_insets(col_strips, bw)

    # the corner radius is a floor, never a ceiling
    left_i, right_i = max(left_i, left_rad), max(right_i, right_rad)
    top_i, bot_i = max(top_i, top_rad), max(bot_i, bot_rad)

    left_i, right_i = _reject_runaway(left_i, right_i, bw)
    top_i, bot_i = _reject_runaway(top_i, bot_i, bh)

    left_i, right_i = _symmetry_snap(left_i, right_i, bw)
    top_i, bot_i = _symmetry_snap(top_i, bot_i, bh)

    # keep a small stretchable centre band so the slice stays valid in Unity
    def _clamp(lead: int, trail: int, span: int) -> tuple[int, int]:
        min_center = max(2, int(0.06 * span))
        if lead + trail > span - min_center:
            scale = (span - min_center) / max(1, lead + trail)
            lead, trail = int(lead * scale), int(trail * scale)
        return lead, trail

    left_i, right_i = _clamp(left_i, right_i, bw)
    top_i, bot_i = _clamp(top_i, bot_i, bh)

    return (
        {
            "l": max(1, x0 + left_i),
            "t": max(1, y0 + top_i),
            "r": max(1, (w - 1 - x1) + right_i),
            "b": max(1, (h - 1 - y1) + bot_i),
        },
        {"v_run": v_run is not None, "h_run": h_run is not None},
    )


# Two borders on the same axis may differ by at most this ratio for the axis to count as
# a frame. Real UI frames are symmetric — the generation prompts even demand it — so a
# left border three times the right one means the detector locked onto interior content.
_FRAME_SYMMETRY_RATIO = 0.5


def _axis_is_frame(lead: int, trail: int, span: int, has_run: bool) -> bool:
    """Whether one axis's borders describe a stretchable frame.

    Four things must hold, and each rules out a distinct way of being wrong:

    * a **real uniform run** on this axis — borders from the gradient/shading fallback are
      a guess about where a seam might be, not evidence of a band that survives stretching;
    * neither border past `_MAX_BORDER_FRAC`, which catches one side running away into the
      interior;
    * the two borders roughly **symmetric**, the signature of a frame drawn around an
      interior rather than of the detector latching onto off-centre content;
    * and what they leave between them at least `_MIN_CENTER_FRAC` of the span — the check
      the per-side cap structurally cannot make, since two borders can each sit just under
      the cap and still consume four fifths of the sprite between them.
    """
    if not has_run or span <= 0:
        return False
    hi = max(lead, trail)
    if hi <= 0 or hi > _MAX_BORDER_FRAC * span:
        return False
    if (span - lead - trail) < _MIN_CENTER_FRAC * span:
        return False
    return (min(lead, trail) / hi) >= _FRAME_SYMMETRY_RATIO


def is_sliceable(img: Image.Image) -> bool:
    """Whether this sprite is actually a resizable frame, as opposed to a picture that
    merely happens to be rectangular.

    9-slicing something with no uniform interior is actively harmful: the "border" lands
    on real content and stretching smears it. Measured against the reference screenshot,
    the ClashUp nav bar (five icons in a row, no stretchable band anywhere) scored 60/100
    sliced versus 88 stretched, and a round level badge scored 33 sliced versus 87.

    Baked-in lettering makes this common rather than exotic. Text erasure is off by
    default, so a currency pill carries its amount and a button carries its caption — and
    glyphs break line-to-line uniformity, so the longest uniform run collapses to whatever
    gap sits between the icon and the first letter. The borders that fall out of that are
    arbitrary, which is exactly what `_axis_is_frame` is there to notice.
    """
    rgba = np.asarray(img.convert("RGBA"))
    h, w = rgba.shape[:2]
    b, evidence = _detect(img)
    return (
        _axis_is_frame(b["l"], b["r"], w, evidence["h_run"])
        or _axis_is_frame(b["t"], b["b"], h, evidence["v_run"])
    )


def detect_borders_if_frame(img: Image.Image) -> dict | None:
    """Borders for a sprite that is a frame, or None for one that isn't.

    This is what the pipeline should call when deciding an asset's `nine_slice`; the bare
    `detect_borders` always returns numbers and is for the manual override endpoint, where
    the user has already decided the thing is sliceable."""
    borders, evidence = _detect(img)
    h, w = img.size[1], img.size[0]
    if _axis_is_frame(borders["l"], borders["r"], w, evidence["h_run"]) or _axis_is_frame(
        borders["t"], borders["b"], h, evidence["v_run"]
    ):
        return borders
    return None


def detect_from_file(path) -> dict:
    with Image.open(path) as img:
        return detect_borders(img)
