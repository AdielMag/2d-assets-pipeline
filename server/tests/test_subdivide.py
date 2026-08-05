import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw

from app.processing.subdivide import (
    has_children, is_already_covered, repeat_units, to_screen_pct,
)


def _pill(w=280, h=110, fill=(38, 148, 160), rim=(214, 176, 84), gem=None):
    """A capsule with a gold rim, optionally with a gem sitting on its left third."""
    img = Image.new("RGB", (w, h), (18, 20, 26))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, w - 3, h - 3], radius=h // 2, fill=fill, outline=rim, width=5)
    if gem is not None:
        cx, cy, r = int(w * 0.18), h // 2, int(h * 0.28)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=gem)
    return img


def _stack(tiles, gap=18, bg=(18, 20, 26)):
    """Stack tiles vertically with a quiet gap between them, as one crop."""
    w = max(t.width for t in tiles)
    h = sum(t.height for t in tiles) + gap * (len(tiles) - 1)
    out = Image.new("RGB", (w, h), bg)
    y = 0
    for t in tiles:
        out.paste(t, (0, y))
        y += t.height + gap
    return out


def test_two_identical_pills_in_one_box_read_as_a_repeat():
    """The reported bug: one detected box around a two-row currency capsule. Both rows
    are the same artwork, so this is a repeat and each row should become its own element."""
    crop = _stack([_pill(gem=(150, 70, 210)), _pill(gem=(150, 70, 210))])
    found = repeat_units(crop)
    assert found is not None, "two stacked identical pills should read as a repeat"
    axis, units = found
    assert axis == "vertical"
    assert len(units) == 2
    # Each unit should be roughly half the crop, not a sliver.
    for _x, _y, _w, unit_h in units:
        assert unit_h > crop.height * 0.3


def test_a_single_pill_is_not_a_repeat():
    """A lone pill has one band. Splitting it would be wrong, and silence is the safe
    failure direction — a missed split costs a click, a wrong one writes bad rows."""
    assert repeat_units(_pill(gem=(150, 70, 210))) is None


def test_differing_tiles_are_not_a_repeat():
    """Five different nav icons in a row are a *container*, not a repeat: they must not be
    collapsed into 'N copies of one thing'. Distinct artwork must fail the hash check."""
    tiles = [
        _pill(w=200, h=100, fill=c, gem=None)
        for c in [(200, 60, 60), (60, 200, 90), (60, 90, 200), (220, 200, 60)]
    ]
    found = repeat_units(_stack(tiles))
    assert found is None, "visually distinct tiles must not be reported as repeats"


def test_flat_crop_is_not_a_repeat():
    """A plain fill has no fingerprint — perceptual_hash refuses it — so a banded
    gradient or empty panel can never be mistaken for repeated instances."""
    assert repeat_units(Image.new("RGB", (300, 300), (90, 90, 90))) is None


def test_to_screen_pct_maps_a_child_into_the_parents_box():
    # Parent occupies the top-right; child is the left third of the parent's crop.
    parent = (60.0, 4.0, 20.0, 6.0)
    x, y, w, h = to_screen_pct(parent, 0.0, 25.0, 30.0, 50.0)
    assert x == 60.0
    assert y == 5.5           # 4 + 0.25 * 6
    assert w == 6.0           # 0.30 * 20
    assert h == 3.0           # 0.50 * 6


def test_already_decomposed_parents_are_left_alone():
    parent = (10.0, 10.0, 20.0, 20.0)
    child = (12.0, 12.0, 5.0, 5.0)
    peer = (40.0, 10.0, 20.0, 20.0)
    assert has_children(parent, [child, peer]) is True
    assert has_children(parent, [peer]) is False


def test_a_proposed_child_that_already_exists_is_not_offered_again():
    existing = (12.0, 12.0, 5.0, 5.0)
    assert is_already_covered((12.1, 12.1, 5.0, 5.0), [existing]) is True
    assert is_already_covered((30.0, 30.0, 5.0, 5.0), [existing]) is False
