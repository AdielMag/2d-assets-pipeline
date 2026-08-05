import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image, ImageDraw

from app.processing.nine_slice import (
    _axis_is_frame, detect_borders, detect_borders_if_frame,
)

_ALPHA = 16


def _rounded_gradient(size, radius, top=(255, 255, 255), bottom=(0, 0, 0)):
    """A rounded-rect fully opaque within its silhouette, shaded with a steep top-to-
    bottom gradient (per-row delta above the old fixed band threshold) so the old
    fixed-threshold scan would misread nearly every row as a shading seam."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    for y in range(size):
        t = y / (size - 1)
        rgb[y, :, :] = [int(top[c] + (bottom[c] - top[c]) * t) for c in range(3)]

    rgba = np.dstack([rgb, np.asarray(mask)])
    return Image.fromarray(rgba, "RGBA")


def _expected_corner_inset(opaque_line_counts: np.ndarray) -> int:
    """Ground truth for 'the shape has reached its full extent': the same plateau
    definition detect_borders uses, computed independently here so the test isn't
    just asserting the implementation against itself for the *radius* value — it
    asserts the gradient contributes nothing beyond what the alpha silhouette alone
    would give, which is exactly the bug being fixed."""
    peak = int(opaque_line_counts.max())
    thresh = peak - max(1, round(0.005 * peak))
    reached = opaque_line_counts >= thresh
    return int(np.argmax(reached))


def test_detect_borders_ignores_gradient_uses_corner_radius():
    size, radius = 30, 6  # chosen so per-row gradient delta (~9) clears the old fixed
                          # threshold (8.0) on nearly every row
    img = _rounded_gradient(size, radius)
    opaque = np.asarray(img.convert("RGBA"))[..., 3] > _ALPHA
    expected = _expected_corner_inset(opaque.sum(axis=1))
    assert expected < size // 4, "test setup: corner inset should be a small fraction of the image"

    borders = detect_borders(img)

    # the steep gradient must not be mistaken for shading seams: borders should track
    # the corner silhouette exactly, not cut deep into the gradient (the pre-fix
    # behavior chewed roughly half the image into the border on this fixture)
    assert borders["t"] == expected
    assert borders["b"] == expected
    assert borders["l"] == expected
    assert borders["r"] == expected


def test_detect_borders_still_finds_a_real_shading_seam():
    size, radius, seam = 40, 6, 10
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    rgb[:seam, :, :] = (240, 240, 240)   # flat light cap
    rgb[seam:, :, :] = (60, 60, 60)      # flat darker body

    rgba = np.dstack([rgb, np.asarray(mask)])
    img = Image.fromarray(rgba, "RGBA")

    borders = detect_borders(img)

    # a genuine sharp seam should still be picked up and win over the smaller corner radius
    assert borders["t"] >= seam - 1


def test_two_symmetric_borders_can_still_swallow_the_stretchable_band():
    """The gap every per-side check structurally misses.

    Each border is tested against _MAX_BORDER_FRAC on its own and the pair against a
    symmetry ratio — and two borders of 39% pass both while leaving 22% of the sprite to
    stretch. That is not a frame. The real nav bar came back l=348 r=348 on an 871px
    sprite: symmetry a perfect 1.0, each side a hair under the cap, 80% border. Sliced on
    those numbers it scored 82 where stretching scored 90.
    """
    span = 100
    assert not _axis_is_frame(39, 39, span, has_run=True), (
        "39%+39% leaves a 22% band — too little to be what 9-slice is for"
    )
    assert _axis_is_frame(20, 20, span, has_run=True), (
        "20%+20% leaves 60% stretchable, which is an ordinary frame"
    )
    # The other guards must keep working alongside it.
    assert not _axis_is_frame(20, 20, span, has_run=False), "no uniform run, no slicing"
    assert not _axis_is_frame(5, 30, span, has_run=True), "asymmetric borders are not a frame"
    assert not _axis_is_frame(45, 5, span, has_run=True), "a runaway border is not a frame"


def test_a_picture_with_no_stretchable_band_is_not_sliced():
    """A row of icons on a bar has no band that survives stretching anywhere along it.
    `detect_borders` will always return numbers — that is what the manual override endpoint
    wants — so the decision of whether to USE them belongs to `detect_borders_if_frame`."""
    w, h = 240, 60
    img = Image.new("RGBA", (w, h), (58, 132, 220, 255))
    d = ImageDraw.Draw(img)
    # Round icons, not squares. A solid rectangle spans a run of identical rows and is
    # therefore genuinely stretchable along that axis — the detector is right to say so.
    # Real icons are not rectangles, and a circle's every row differs from the last, so
    # nowhere on either axis is there a band that survives being pulled.
    for cx in range(30, w, 60):
        d.ellipse([cx - 14, 16, cx + 14, 44], fill=(240, 200, 60, 255))
    assert detect_borders_if_frame(img) is None, (
        "a bar of icons has no stretchable middle and must render stretched, not sliced"
    )
