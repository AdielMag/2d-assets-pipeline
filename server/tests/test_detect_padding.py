"""The outward margin on a detected box.

Detection lands short. Both the vision model's box and the gradient snap aim at an
element's hard painted edge and routinely stop inside it, so what is left outside the box
is real artwork — a badge's ring, a pill's bottom rim, a frame's outer bevel — and no later
stage can recover pixels that were never in the crop. The margin exists to cover that.

The tension is that a margin big enough to do the job is also big enough to reach the
element next door, and an overlap is read downstream as "this is drawn on that"
(`_resolve_containment`), which would have a padded nav icon mark its neighbour as a
background to be inpainted empty. So: grow generously, but never past half the gap.
"""
import numpy as np
import pytest
from PIL import Image

from app.processing.region_detector import (
    OUTWARD_PAD_MAX_PX,
    _neighbour_limited_pad,
    filter_and_refine_regions,
)

W, H = 400, 300


def test_a_lone_box_grows_on_every_side():
    (x, y, w, h) = _neighbour_limited_pad([(100, 100, 80, 60)], W, H)[0]
    assert x < 100 and y < 100
    assert x + w > 180 and y + h > 160


def test_neighbours_never_end_up_overlapping():
    """Two elements side by side with a narrow gap. Both want to grow into it; neither may
    take more than its half, so the gap shrinks but never closes."""
    gap = 10
    a, b = (100, 100, 80, 60), (100 + 80 + gap, 100, 80, 60)
    (ax, ay, aw, ah), (bx, by, bw, bh) = _neighbour_limited_pad([a, b], W, H)
    assert ax + aw <= bx, "padded boxes collided — the half-gap share was overspent"
    assert ax + aw > a[0] + a[2], "the left box did not use its share"
    assert bx < b[0], "the right box did not use its share"


def test_touching_neighbours_do_not_grow_towards_each_other():
    a, b = (100, 100, 80, 60), (180, 100, 80, 60)  # flush, zero gap
    (ax, ay, aw, ah), (bx, _by, _bw, _bh) = _neighbour_limited_pad([a, b], W, H)
    assert ax + aw == 180 and bx == 180
    assert ay < 100, "a blocked side must not block the free ones"


def test_a_child_may_pad_against_its_container_and_the_container_against_it():
    """The rule is about elements *beside* each other. A nav icon on a nav bar overlaps it
    by design, and neither should be held back by the other — being contained is not a
    collision to protect against."""
    bar, icon = (0, 200, 400, 80), (40, 215, 50, 50)
    (bx, by, bw, bh), (ix, iy, iw, ih) = _neighbour_limited_pad([bar, icon], W, H)
    assert ix < 40 and ix + iw > 90, "the icon was pinned by the bar it sits on"
    assert by < 200 and by + bh > 280, "the bar was pinned by the icon sitting on it"


def test_the_margin_is_capped_on_a_huge_element():
    """Proportional growth is right for a button and absurd for a full-screen bar."""
    (x, _y, w, _h) = _neighbour_limited_pad([(400, 400, 1600, 200)], 3000, 3000)[0]
    assert 400 - x <= OUTWARD_PAD_MAX_PX
    assert w - 1600 <= 2 * OUTWARD_PAD_MAX_PX


def test_padding_never_pushes_a_box_off_the_screen():
    boxes = _neighbour_limited_pad([(0, 0, 60, 40), (W - 60, H - 40, 60, 40)], W, H)
    for x, y, w, h in boxes:
        assert x >= 0 and y >= 0 and x + w <= W and y + h <= H


@pytest.fixture
def screenshot(tmp_path):
    """Two panels side by side with a 20px gap, on a flat backdrop."""
    img = np.full((H, W, 3), 40, np.uint8)
    img[100:160, 100:180] = (58, 132, 220)
    img[100:160, 200:280] = (58, 132, 220)
    path = tmp_path / "shot.png"
    Image.fromarray(img).save(path)
    return path


def test_refine_grows_the_boxes_it_returns(screenshot):
    """End to end through the real refine path: a box the model placed tight inside its
    element comes back covering more than it went in with."""
    items = [
        {"name": "Left", "box_2d": [int(105 / H * 1000), int(105 / W * 1000),
                                    int(155 / H * 1000), int(175 / W * 1000)]},
    ]
    out = filter_and_refine_regions(screenshot, items, W, H)
    assert len(out) == 1
    got = out[0]
    assert got["x"] * W / 100 < 105
    assert (got["x"] + got["w"]) * W / 100 > 175


def test_a_text_run_keeps_the_old_hairline_margin(screenshot):
    """A text box is not cropped into a sprite directly — it seeds the polish pass's own
    padding (`_pad_box` in routers/mockups.py) before that crops a reference image for the
    LLM. Growing it here only hands that pass more frame to misread: at the element
    margin, measured on the ClashUp lobby, the gold amount's crop stopped being the digits
    and became the whole pill interior."""
    box = [int(102 / H * 1000), int(102 / W * 1000), int(158 / H * 1000), int(178 / W * 1000)]
    as_element = filter_and_refine_regions(screenshot, [{"name": "A", "box_2d": box}], W, H)[0]
    as_text = filter_and_refine_regions(
        screenshot, [{"name": "A", "type": "text", "text": "hi", "box_2d": box}], W, H
    )[0]
    assert as_text["w"] < as_element["w"] and as_text["h"] < as_element["h"]


def test_refine_keeps_two_detected_elements_apart(screenshot):
    """The failure this guards: padded boxes touching turns two independent elements into
    a container-and-occluder pair at build time."""
    items = [
        {"name": "Left", "box_2d": [int(102 / H * 1000), int(102 / W * 1000),
                                    int(158 / H * 1000), int(178 / W * 1000)]},
        {"name": "Right", "box_2d": [int(102 / H * 1000), int(202 / W * 1000),
                                     int(158 / H * 1000), int(278 / W * 1000)]},
    ]
    out = {i["name"]: i for i in filter_and_refine_regions(screenshot, items, W, H)}
    assert set(out) == {"Left", "Right"}
    left_right_edge = (out["Left"]["x"] + out["Left"]["w"])
    assert left_right_edge <= out["Right"]["x"] + 1e-9
