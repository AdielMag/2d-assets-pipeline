import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app.processing.composite import nine_slice_resize


def _swatch_frame(size=20, border=5):
    """A frame whose 9 patches are each a distinct flat color, so we can tell exactly
    which source pixels ended up where after nine_slice_resize."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    colors = {
        "tl": (255, 0, 0, 255), "tr": (0, 255, 0, 255),
        "bl": (0, 0, 255, 255), "br": (255, 255, 0, 255),
        "edge": (128, 128, 128, 255), "center": (10, 20, 30, 255),
    }
    for y in range(size):
        for x in range(size):
            in_top, in_bottom = y < border, y >= size - border
            in_left, in_right = x < border, x >= size - border
            if in_top and in_left:
                c = colors["tl"]
            elif in_top and in_right:
                c = colors["tr"]
            elif in_bottom and in_left:
                c = colors["bl"]
            elif in_bottom and in_right:
                c = colors["br"]
            elif in_top or in_bottom or in_left or in_right:
                c = colors["edge"]
            else:
                c = colors["center"]
            px[x, y] = c
    return img, colors


def test_nine_slice_resize_preserves_corners_and_scales_center():
    size, border = 20, 5
    img, colors = _swatch_frame(size, border)
    b = {"l": border, "t": border, "r": border, "b": border}

    target_w, target_h = 50, 40  # larger than source; border well under target/3
    out = nine_slice_resize(img, b, target_w, target_h)

    assert out.size == (target_w, target_h)
    # corners keep their native color, unscaled and unblended
    assert out.getpixel((0, 0)) == colors["tl"]
    assert out.getpixel((target_w - 1, 0)) == colors["tr"]
    assert out.getpixel((0, target_h - 1)) == colors["bl"]
    assert out.getpixel((target_w - 1, target_h - 1)) == colors["br"]
    # the stretched center still holds the center color, not corner/edge bleed
    assert out.getpixel((target_w // 2, target_h // 2)) == colors["center"]


def test_nine_slice_resize_clamps_oversized_borders():
    size, border = 20, 9  # border pair (18) leaves only 2px of center in a 20px source
    img, colors = _swatch_frame(size, border)
    b = {"l": border, "t": border, "r": border, "b": border}

    # shrinking well below the source size forces proportional overlap scaling (18 > 12)
    out = nine_slice_resize(img, b, 12, 12)

    assert out.size == (12, 12)
    assert out.getpixel((0, 0)) == colors["tl"]
    assert out.getpixel((11, 11)) == colors["br"]


def test_nine_slice_resize_does_not_clamp_when_borders_exceed_third_of_span():
    size, border = 20, 8  # 8 + 8 = 16 <= 20
    img, colors = _swatch_frame(size, border)
    b = {"l": border, "t": border, "r": border, "b": border}

    # target 20x20: border (8) is greater than target//3 (6), but 8+8=16 fits inside 20 without overlapping.
    # It must NOT be clamped down to 6px.
    out = nine_slice_resize(img, b, 20, 20)
    assert out.size == (20, 20)
    # Check pixel at (7, 7) - in 8px border it should still be top-left corner color, not center color
    assert out.getpixel((7, 7)) == colors["tl"]
    # Check pixel at (10, 10) - center color
    assert out.getpixel((10, 10)) == colors["center"]
