"""Auto-detect and refine UI region bounding boxes from mobile game screens.

Combines Vision LLM detection with OpenCV gradient-based per-edge snapping
and Non-Maximum Suppression (NMS) for pixel-precise region bounds.
"""

from pathlib import Path
import cv2
import numpy as np
from PIL import Image

# Outward margin added to every refined box so nothing of the element is left outside it.
#
# This is sized for the error actually observed, not for antialiasing. Measured against the
# ClashUp lobby, the boxes that come out of the vision model and the gradient snap sit 5-15%
# short of their element and skewed up-left: the level badge kept the top-left two thirds of
# its gold ring, both currency pills lost their bottom rim, the PLAY frame lost its right
# edge, and every nav icon cut through its own artwork. The old 1.5%/2px margin was sized
# for a soft shadow and could not cover any of it.
#
# Over-padding is the cheap direction to err in. Extra background inside a box is removed by
# segmentation at extract time, and the sprite still fills its rect because the rect grew
# with it; a clipped element, by contrast, is missing pixels no later stage can recover.
# What over-padding must NOT do is reach a neighbour — see `_neighbour_limited_pad`.
OUTWARD_PAD_FRAC = 0.06
OUTWARD_PAD_MIN_PX = 6
# Past this the margin has stopped tracking the element's own soft edge and is just
# swallowing screen. A full-width nav bar does not need 46px of air.
OUTWARD_PAD_MAX_PX = 24

# Text runs keep the old hairline margin, and the reason is that a text box is not used the
# way an element box is. It is not cropped to make a sprite directly; it is the seed box
# the polish pass (`_pad_box` in routers/mockups.py) pads again before cropping a reference
# image for the LLM. Padding it here too just hands that pass more frame to mistake for
# lettering, and measured on this screen it did exactly that: at the element margin the
# gold amount's crop stopped being the digits and became the whole pill interior, and the
# Cards caption's crop drifted up into the icon above it. The later pass's own padding is
# the one that should grow if a caption is being clipped — not this.
TEXT_PAD_FRAC = 0.015
TEXT_PAD_MIN_PX = 2


def parse_and_normalize_items(items: list[dict], img_w: int, img_h: int) -> list[dict]:
    """Normalizes raw LLM output items (which may use xmin/ymin/xmax/ymax in pixels,
    or x/y/w/h in percentages or pixels) to standard pixel coordinates (px, py, pw, ph).
    """
    normalized = []
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue

        px, py, pw, ph = None, None, None, None

        # Preferred format: box_2d = [ymin, xmin, ymax, xmax] normalized to 0-1000. This is
        # the exact convention Gemini-family vision models are TRAINED to output, so asking
        # for it (rather than raw pixels in x,y order) is what makes the boxes land on the
        # elements instead of drifting — the model emits these coordinates natively without
        # any error-prone mental pixel arithmetic or axis reordering.
        # Ref: https://ai.google.dev/gemini-api/docs/image-understanding
        box = item.get("box_2d")
        if px is None and isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                ymin, xmin, ymax, xmax = (float(v) for v in box)
                # We always request the 0-1000 normalized scale; descale to pixels.
                xmin, xmax = xmin / 1000.0 * img_w, xmax / 1000.0 * img_w
                ymin, ymax = ymin / 1000.0 * img_h, ymax / 1000.0 * img_h
                if xmax < xmin:
                    xmin, xmax = xmax, xmin
                if ymax < ymin:
                    ymin, ymax = ymax, ymin
                px = max(0, int(round(xmin)))
                py = max(0, int(round(ymin)))
                pw = max(1, int(round(xmax - xmin)))
                ph = max(1, int(round(ymax - ymin)))
            except (ValueError, TypeError):
                px = None

        # Legacy format: xmin, ymin, xmax, ymax (pixels, or 0-1000 normalized)
        if px is None and all(k in item for k in ("xmin", "ymin", "xmax", "ymax")):
            try:
                xmin = float(item["xmin"])
                ymin = float(item["ymin"])
                xmax = float(item["xmax"])
                ymax = float(item["ymax"])

                # Handle normalized 0-1000 scale if xmax <= 1000 and ymax <= 1000 but larger than 100
                if 1.0 < xmax <= 1000.0 and 1.0 < ymax <= 1000.0 and img_w > 1000:
                    xmin = (xmin / 1000.0) * img_w
                    xmax = (xmax / 1000.0) * img_w
                    ymin = (ymin / 1000.0) * img_h
                    ymax = (ymax / 1000.0) * img_h
                elif xmax <= 1.0 and ymax <= 1.0:
                    xmin *= img_w
                    xmax *= img_w
                    ymin *= img_h
                    ymax *= img_h

                px = max(0, int(round(xmin)))
                py = max(0, int(round(ymin)))
                pw = max(1, int(round(xmax - xmin)))
                ph = max(1, int(round(ymax - ymin)))
            except (ValueError, TypeError):
                pass

        # Fallback to x, y, w, h format
        if px is None and all(k in item for k in ("x", "y", "w", "h")):
            try:
                x = float(item["x"])
                y = float(item["y"])
                w = float(item["w"])
                h = float(item["h"])

                # If values are <= 100, assume percentages; otherwise assume pixels
                if x <= 100 and y <= 100 and w <= 100 and h <= 100:
                    px = max(0, int(round((x / 100.0) * img_w)))
                    py = max(0, int(round((y / 100.0) * img_h)))
                    pw = max(1, int(round((w / 100.0) * img_w)))
                    ph = max(1, int(round((h / 100.0) * img_h)))
                else:
                    px = max(0, int(round(x)))
                    py = max(0, int(round(y)))
                    pw = max(1, int(round(w)))
                    ph = max(1, int(round(h)))
            except (ValueError, TypeError):
                pass

        if px is not None and pw > 4 and ph > 4 and px < img_w and py < img_h:
            item_copy = dict(item)
            item_copy["_px"] = (px, py, pw, ph)
            normalized.append(item_copy)

    return normalized


def compute_gradient_map(img_bgr: np.ndarray) -> np.ndarray:
    """Multi-channel (grayscale + LAB a/b) morphological gradient magnitude for
    the whole image, computed once and reused for every region snap."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    _, a, b = cv2.split(lab)

    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    grad_g = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, k)
    grad_a = cv2.morphologyEx(a, cv2.MORPH_GRADIENT, k)
    grad_b = cv2.morphologyEx(b, cv2.MORPH_GRADIENT, k)
    return cv2.max(grad_g, cv2.max(grad_a, grad_b)).astype(np.float32)


def _find_edge_peak(profile: np.ndarray, lo: int, hi: int, pos: int) -> int:
    """Finds the position in profile[lo:hi) *nearest to `pos`* whose value
    clears a noise-floor threshold, refined to the local peak around that hit.

    Deliberately NOT a global argmax over the window: on painterly game art,
    bold interior content (button text, icons) routinely produces a *stronger*
    gradient response than the element's own soft outer border against a busy
    background. A pure strongest-peak search reliably jumps to that interior
    content instead of the true edge. Nearest-qualifying-peak keeps the search
    anchored close to where the LLM already placed the edge.
    """
    lo = max(0, lo)
    hi = min(len(profile), hi)
    if hi - lo < 2:
        return pos

    window = profile[lo:hi]
    baseline = float(np.median(profile)) if profile.size else 0.0
    mad = float(np.median(np.abs(window - np.median(window))))
    threshold = baseline + max(30.0, mad * 5.0)

    idxs = np.arange(lo, hi)
    order = np.argsort(np.abs(idxs - pos))
    for oi in order:
        idx = int(idxs[oi])
        if profile[idx] >= threshold:
            # Refine to the true local peak around this qualifying hit.
            w0, w1 = max(lo, idx - 2), min(hi, idx + 3)
            return w0 + int(np.argmax(window[w0 - lo : w1 - lo]))
    return pos


def snap_box_to_edges(
    grad: np.ndarray, px: int, py: int, pw: int, ph: int
) -> tuple[int, int, int, int]:
    """Snaps each of the 4 box edges independently to the nearest strong local
    gradient peak. Independent per-edge search (rather than growing/merging
    connected blobs) means a search can't reach across a real gap and absorb a
    neighboring element — it only ever finds the boundary closest to where the
    LLM already placed that edge.

    The search is deliberately asymmetric: generous outward (an undersized LLM
    box needs to grow to reach the true edge, and the area just outside a box
    is normally background) but only a small nudge inward (the area just
    inside a box is normally the element's own content — text, icons — which
    must never be mistaken for the boundary).
    """
    img_h, img_w = grad.shape[:2]
    x0, y0, x1, y1 = px, py, px + pw, py + ph

    out_x = min(60, max(10, int(pw * 0.25)))
    in_x = min(15, max(4, int(pw * 0.06)))
    out_y = min(60, max(10, int(ph * 0.25)))
    in_y = min(15, max(4, int(ph * 0.06)))

    # Trim the perpendicular band so corner artifacts (rounded corners, drop
    # shadows) don't dominate the profile.
    band_y0 = min(img_h - 1, max(0, y0 + int(ph * 0.15)))
    band_y1 = max(band_y0 + 1, min(img_h, y1 - int(ph * 0.15)))
    band_x0 = min(img_w - 1, max(0, x0 + int(pw * 0.15)))
    band_x1 = max(band_x0 + 1, min(img_w, x1 - int(pw * 0.15)))

    # Left/right: sum gradient over the vertical band -> profile indexed by column.
    col_profile = grad[band_y0:band_y1, :].sum(axis=0)
    new_x0 = _find_edge_peak(col_profile, x0 - out_x, x0 + in_x, x0)
    new_x1 = _find_edge_peak(col_profile, x1 - in_x, x1 + out_x, x1)

    # Top/bottom: sum gradient over the horizontal band -> profile indexed by row.
    row_profile = grad[:, band_x0:band_x1].sum(axis=1)
    new_y0 = _find_edge_peak(row_profile, y0 - out_y, y0 + in_y, y0)
    new_y1 = _find_edge_peak(row_profile, y1 - in_y, y1 + out_y, y1)

    if new_x1 - new_x0 < 4:
        new_x0, new_x1 = x0, x1
    if new_y1 - new_y0 < 4:
        new_y0, new_y1 = y0, y1

    final_x = max(0, new_x0)
    final_y = max(0, new_y0)
    final_w = min(img_w, new_x1) - final_x
    final_h = min(img_h, new_y1) - final_y

    # Sanity check: keep box size within reasonable bounds relative to proposal.
    if 0.5 * pw <= final_w <= 1.6 * pw and 0.5 * ph <= final_h <= 1.6 * ph:
        return final_x, final_y, final_w, final_h

    return px, py, pw, ph


def _pad_for(size: int, is_text: bool = False) -> int:
    """How far one side of a box of this size may grow, before neighbours are considered."""
    if is_text:
        return int(max(TEXT_PAD_MIN_PX, round(size * TEXT_PAD_FRAC)))
    return int(min(OUTWARD_PAD_MAX_PX, max(OUTWARD_PAD_MIN_PX, round(size * OUTWARD_PAD_FRAC))))


def _neighbour_limited_pad(
    boxes: list[tuple[int, int, int, int]], img_w: int, img_h: int,
    is_text: list[bool] | None = None,
) -> list[tuple[int, int, int, int]]:
    """Grow every box outward, but never by more than half the free space to whatever sits
    beside it on that side.

    A margin big enough to actually cover a clipped element is also big enough to reach the
    element next to it, and a box that overlaps its neighbour is worse than one that clips
    its own edge: overlap is what `_resolve_containment` reads as "this thing is drawn on
    that thing", so a padded nav icon could mark the icon beside it as a background to be
    inpainted empty. Half the gap is the share that makes that impossible by construction —
    both neighbours can take their half and still not touch.

    Only boxes genuinely *beside* this one count. A container (which starts before this box
    and ends after it) and a child (which sits wholly within it) both fail the beside test,
    so a nav icon is free to pad against the bar it sits on, and the bar against the icons.
    """
    out = []
    for i, (px, py, pw, ph) in enumerate(boxes):
        ax0, ay0, ax1, ay1 = px, py, px + pw, py + ph
        text = bool(is_text[i]) if is_text else False
        left = right = _pad_for(pw, text)
        top = bottom = _pad_for(ph, text)
        for j, (qx, qy, qw, qh) in enumerate(boxes):
            if i == j:
                continue
            bx0, by0, bx1, by1 = qx, qy, qx + qw, qy + qh
            if by1 > ay0 and by0 < ay1:  # shares rows: a left/right neighbour
                if bx1 <= ax0:
                    left = min(left, (ax0 - bx1) // 2)
                elif bx0 >= ax1:
                    right = min(right, (bx0 - ax1) // 2)
            if bx1 > ax0 and bx0 < ax1:  # shares columns: a top/bottom neighbour
                if by1 <= ay0:
                    top = min(top, (ay0 - by1) // 2)
                elif by0 >= ay1:
                    bottom = min(bottom, (by0 - ay1) // 2)
        nx0, ny0 = max(0, ax0 - max(0, left)), max(0, ay0 - max(0, top))
        nx1, ny1 = min(img_w, ax1 + max(0, right)), min(img_h, ay1 + max(0, bottom))
        out.append((nx0, ny0, nx1 - nx0, ny1 - ny0))
    return out


def perceptual_hash(image: Image.Image) -> int | None:
    """64-bit DCT perceptual hash of an image. Recognizes visually-identical region
    crops (e.g. a shared button background reused across a screen) even when their pixel
    dimensions differ slightly. Compare two hashes with `hamming` — a small distance
    (<= ~6 bits) means the same artwork."""
    try:
        arr = np.asarray(
            image.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float32
        )
    except Exception:
        return None
    # Near-flat crops (a plain fill with no border/icon/text) carry no reliable
    # fingerprint — every one would hash to 0 and be treated as identical. Refuse to
    # hash them so the caller keeps them as distinct, un-merged elements.
    if float(arr.std()) < 4.0:
        return None
    dct = cv2.dct(arr)
    low = dct[:8, :8].flatten()
    med = float(np.median(low[1:]))  # drop the DC term so flat crops still spread bits
    h = 0
    for value in low:
        h = (h << 1) | int(value > med)
    return h


def hamming(a: int, b: int) -> int:
    """Bit distance between two perceptual hashes."""
    return bin(a ^ b).count("1")


def containment_ratio(
    inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]
) -> float:
    """Fraction of `inner`'s area that falls inside `outer` (0..1). Unlike IoU this stays
    high when a small box is fully swallowed by a much larger one — the signal that a box
    is a child element sitting inside a container panel."""
    ax, ay, aw, ah = inner
    bx, by, bw, bh = outer
    xA, yA = max(ax, bx), max(ay, by)
    xB, yB = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, xB - xA) * max(0, yB - yA)
    area_inner = aw * ah
    return inter / area_inner if area_inner > 0 else 0.0


def compute_iou(boxA: tuple[int, int, int, int], boxB: tuple[int, int, int, int]) -> float:
    """Computes Intersection over Union (IoU) of two boxes (x, y, w, h)."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[0] + boxA[2], boxB[0] + boxB[2])
    yB = min(boxA[1] + boxA[3], boxB[1] + boxB[3])

    interW = max(0, xB - xA)
    interH = max(0, yB - yA)
    interArea = interW * interH

    areaA = boxA[2] * boxA[3]
    areaB = boxB[2] * boxB[3]
    unionArea = areaA + areaB - interArea

    if unionArea <= 0:
        return 0.0
    return interArea / unionArea


def filter_and_refine_regions(
    img_path: Path, items: list[dict], img_w: int, img_h: int
) -> list[dict]:
    """Parses raw LLM detected items, applies OpenCV edge snapping, performs NMS
    suppression of duplicates, and calculates precise percentage coordinates (0..100).
    """
    normalized = parse_and_normalize_items(items, img_w, img_h)
    if not normalized:
        return []

    # Read image for OpenCV processing; compute the gradient map once and
    # reuse it for every region instead of recomputing per-ROI.
    img_bgr = cv2.imread(str(img_path))
    grad = compute_gradient_map(img_bgr) if img_bgr is not None else None

    snapped = []
    for item in normalized:
        px, py, pw, ph = item["_px"]
        if grad is not None:
            px, py, pw, ph = snap_box_to_edges(grad, px, py, pw, ph)
        snapped.append((px, py, pw, ph))

    # Grow every box outward. Both the LLM box and the gradient snap aim at the element's
    # hard painted edge and routinely land inside it, so what is left outside is real
    # artwork — a badge's ring, a pill's bottom rim, a frame's outer bevel — plus whatever
    # soft edge (shadow, glow, antialiasing) sits beyond that. Padded as a set rather than
    # one at a time, because how far a box may grow depends on what is next to it, and text
    # runs are flagged so they keep their own much smaller margin (see TEXT_PAD_FRAC).
    is_text = [str(i.get("type") or "").strip().lower() == "text" for i in normalized]
    padded = _neighbour_limited_pad(snapped, img_w, img_h, is_text)

    refined_items = []
    for item, (px, py, pw, ph) in zip(normalized, padded):
        # Convert to percentage space (0..100) with 2 decimal precision
        x_pct = round((px / float(img_w)) * 100.0, 2)
        y_pct = round((py / float(img_h)) * 100.0, 2)
        w_pct = round((pw / float(img_w)) * 100.0, 2)
        h_pct = round((ph / float(img_h)) * 100.0, 2)

        # Clamp bounds
        x_pct = max(0.0, min(99.0, x_pct))
        y_pct = max(0.0, min(99.0, y_pct))
        w_pct = max(0.5, min(100.0 - x_pct, w_pct))
        h_pct = max(0.5, min(100.0 - y_pct, h_pct))

        item["x"] = x_pct
        item["y"] = y_pct
        item["w"] = w_pct
        item["h"] = h_pct
        item["_px"] = (px, py, pw, ph)
        refined_items.append(item)

    # Non-Maximum Suppression (NMS) to eliminate heavy overlapping duplicate boxes
    final_items = []
    for item in refined_items:
        boxA = item["_px"]
        duplicate = False
        for kept in final_items:
            boxB = kept["_px"]
            iou = compute_iou(boxA, boxB)
            # If > 75% overlap, skip duplicate
            if iou > 0.75:
                duplicate = True
                break
        if not duplicate:
            final_items.append(item)

    # NB: containment is resolved later, at BUILD time, from the stored region rectangles
    # (see `_resolve_containment` in routers/mockups.py) rather than here — that way it also
    # reflects any boxes the user has hand-edited in the UI. A box that fully encloses
    # smaller boxes is a BACKGROUND generated as an empty frame; the enclosed boxes become
    # their own foreground sprites. So here we KEEP every surviving box (container + its
    # children); we do not drop the inner ones.
    for item in final_items:
        item.pop("_px", None)

    return final_items
