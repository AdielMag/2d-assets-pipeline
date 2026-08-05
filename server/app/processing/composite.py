"""Composite a screen layout (elements with normalized rects) into one image. Shared by
the standalone CLI verification tool and the in-app screen preview endpoint so there is
one implementation of "how a screen.json becomes a picture"."""
import numpy as np
from PIL import Image


def hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    v = value.lstrip("#")
    if len(v) == 6:
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), 255
    if len(v) == 8:
        return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4, 6))
    raise ValueError(f"bad hex color: {value}")


def checkerboard(w: int, h: int, cell: int = 32) -> Image.Image:
    ys, xs = np.mgrid[0:h, 0:w]
    mask = ((xs // cell) + (ys // cell)) % 2 == 1
    arr = np.full((h, w, 4), (235, 235, 235, 255), dtype=np.uint8)
    arr[mask] = (200, 200, 200, 255)
    return Image.fromarray(arr, "RGBA")


def _clamp_border(lead: int, trail: int, span: int) -> tuple[int, int]:
    """Shrink a border pair that would otherwise overlap in the target span, scaling down
    proportionally if the pair exceeds the available span."""
    if lead + trail > span:
        scale = span / max(1, lead + trail)
        lead, trail = int(lead * scale), int(trail * scale)
    return lead, trail


def nine_slice_resize(img: Image.Image, border: dict, target_w: int, target_h: int) -> Image.Image:
    """Scale-9-with-fill resize: corners keep their native size (clamped only if overlapping),
    edges stretch along one axis, the center stretches both ways. Mirrors CSS border-image
    and Unity 9-slice behavior so sliced assets render identically across all tools."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    l = max(0, min(border.get("l", 0), w))
    t = max(0, min(border.get("t", 0), h))
    r = max(0, min(border.get("r", 0), w - l))
    b = max(0, min(border.get("b", 0), h - t))

    dl, dr = _clamp_border(l, r, target_w)
    dt, db = _clamp_border(t, b, target_h)

    canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    cw, ch = target_w - dl - dr, target_h - dt - db  # stretched center size

    col_bounds = [0, l, w - r, w]
    row_bounds = [0, t, h - b, h]
    dst_col_bounds = [0, dl, target_w - dr, target_w]
    dst_row_bounds = [0, dt, target_h - db, target_h]

    for ri in range(3):
        sy0, sy1 = row_bounds[ri], row_bounds[ri + 1]
        dy0, dy1 = dst_row_bounds[ri], dst_row_bounds[ri + 1]
        if sy1 <= sy0 or dy1 <= dy0:
            continue
        for ci in range(3):
            sx0, sx1 = col_bounds[ci], col_bounds[ci + 1]
            dx0, dx1 = dst_col_bounds[ci], dst_col_bounds[ci + 1]
            if sx1 <= sx0 or dx1 <= dx0:
                continue
            patch = rgba.crop((sx0, sy0, sx1, sy1))
            pw, ph = dx1 - dx0, dy1 - dy0
            if patch.size != (pw, ph):
                patch = patch.resize((pw, ph), Image.BILINEAR)
            canvas.alpha_composite(patch, (dx0, dy0))

    return canvas


def _contain_resize(img: Image.Image, target_w: int, target_h: int) -> tuple[Image.Image, int, int]:
    """Scale preserving aspect ratio to fit inside (target_w, target_h); returns the
    resized image plus the (x, y) offset to center it within that box."""
    w, h = img.size
    scale = min(target_w / w, target_h / h) if w and h else 1
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    return resized, (target_w - new_w) // 2, (target_h - new_h) // 2


def composite_layout(
    elements: list[dict], width: int, height: int, bg: str | None = None
) -> Image.Image:
    """elements: [{"image": PIL.Image, "rect": {x,y,w,h} normalized 0..1, "z": int,
    "fit": "stretch"|"contain"|"slice", "nine_slice": {l,t,r,b} px | None}, ...]
    rects use a top-left origin. Later z draws on top. "fit" defaults to "stretch"
    (independent x/y resize, the original behavior)."""
    canvas = Image.new("RGBA", (width, height), hex_to_rgba(bg)) if bg else checkerboard(width, height)

    for el in sorted(elements, key=lambda e: e.get("z", 0)):
        r = el["rect"]
        w = max(1, round(r["w"] * width))
        h = max(1, round(r["h"] * height))
        x = round(r["x"] * width)
        y = round(r["y"] * height)
        image = el["image"].convert("RGBA")
        fit = el.get("fit", "stretch")

        if fit == "slice" and el.get("nine_slice"):
            sp = nine_slice_resize(image, el["nine_slice"], w, h)
            canvas.alpha_composite(sp, (x, y))
        elif fit == "contain":
            sp, ox, oy = _contain_resize(image, w, h)
            canvas.alpha_composite(sp, (x + ox, y + oy))
        else:
            sp = image.resize((w, h), Image.LANCZOS)
            canvas.alpha_composite(sp, (x, y))

    return canvas
