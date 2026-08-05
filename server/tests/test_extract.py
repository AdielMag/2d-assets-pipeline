"""Extraction and de-occlusion.

The governing invariant is round-trip: cut an element out of a synthetic screenshot,
draw it back into the rectangle it came from, and the result must reproduce the original
pixels. Every real defect this pipeline has had shows up as a round-trip failure —
a mask that ate the element's border, a hole-fill that returned the whole box opaque, a
trim that shrank the sprite inside its own rect.
"""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.processing.extract import extract_region
from app.processing.fidelity import render_like_compositor, score_asset
from app.processing.inpaint import alpha_to_mask, boxes_to_mask, deocclude
from app.processing.trim import trim_and_pad

BACKDROP = (28, 44, 96)
BOX_PCT = (25.0, 25.0, 50.0, 30.0)  # x, y, w, h


def _screenshot(size=(400, 300), pad=0):
    """A gold-bordered blue panel on a flat navy backdrop — the shape of nearly every
    element this pipeline meets.

    `pad` insets the panel within the detected box. pad=0 is the flush case, where the
    element's own border touches the box edge; that is what a tight detection produces
    and it is the harder one to segment. A positive pad leaves backdrop inside the box,
    so there is something for extraction to actually cut away.
    """
    img = Image.new("RGB", size, BACKDROP)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = _box_px(img)
    # -1 on the far edges because PIL's rectangle coordinates are inclusive: without it the
    # panel's last row and column fall one pixel OUTSIDE the crop, which is a genuinely
    # clipped element and box growth correctly widens the box to recover it.
    d.rounded_rectangle(
        [x0 + pad, y0 + pad, x1 - pad - 1, y1 - pad - 1], 14,
        fill=(58, 132, 220), outline=(214, 176, 62), width=6,
    )
    return img


def _box_px(img):
    w, h = img.size
    x, y, bw, bh = BOX_PCT
    return (int(w * x / 100), int(h * y / 100),
            int(w * (x + bw) / 100), int(h * (y + bh) / 100))


def test_extraction_round_trips_to_its_own_rect():
    shot = _screenshot()
    cut, backend, _ = extract_region(shot, BOX_PCT)
    assert backend in {"classical", "sam2", "box"}

    box = _box_px(shot)
    assert cut.size == (box[2] - box[0], box[3] - box[1]), (
        "an extraction must come back the exact size of its rect — the compositor places "
        "it there by rect, so any other size is silently rescaled art"
    )
    result = score_asset(shot.crop(box), cut, fit="stretch", background_rgb=BACKDROP)
    assert result["score"] > 85, result


def test_extraction_keeps_the_element_border():
    """Regression: background was seeded from the outer ring *inside* the box. A detected
    box is tight, so the element's own border sits exactly there and GrabCut learned
    'gold border = background', eroding the frame it was asked to keep."""
    shot = _screenshot()
    cut, _, _ = extract_region(shot, BOX_PCT)
    arr = np.asarray(cut.convert("RGBA"))
    opaque = arr[:, :, 3] > 128
    gold = (np.abs(arr[:, :, :3].astype(int) - np.array([214, 176, 62])).sum(axis=2) < 90)
    assert (gold & opaque).sum() > 0.05 * opaque.sum(), "the gold border was eaten"


def test_extraction_does_not_return_the_whole_box_opaque():
    """Regression: `_clean_mask`'s hole fill flood-filled from (0, 0) assuming that pixel
    was background. When the mask touched the corner it swallowed the entire image, and
    the metric could not see it — an opaque rectangle *is* the reference crop."""
    shot = _screenshot(pad=14)
    cut, _, _ = extract_region(shot, BOX_PCT)
    coverage = (np.asarray(cut.convert("RGBA"))[:, :, 3] > 128).mean()
    assert coverage < 0.90, "extraction returned an uncut rectangle"
    assert coverage > 0.30, "extraction cut away the element itself"


def test_padding_an_extraction_is_what_shrinks_it_in_its_rect():
    """Why `_finalize_version(box_aligned=True)` exists. `trim_and_pad` is right for a
    generation (a subject floating in the provider's frame) and ruinous for an extraction
    (already rect-aligned): the padded sprite drawn back into the rect renders the art
    undersized, which cost ~36 points of mean fidelity until it was found."""
    shot = _screenshot()
    box = _box_px(shot)
    cut, _, _ = extract_region(shot, BOX_PCT)
    rect = (box[2] - box[0], box[3] - box[1])

    def painted_size(asset):
        bbox = render_like_compositor(asset, *rect, fit="contain").getchannel("A").getbbox()
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    aligned_w, aligned_h = painted_size(cut)
    padded_w, padded_h = painted_size(trim_and_pad(cut))

    assert aligned_w >= rect[0] - 2 and aligned_h >= rect[1] - 2, (
        "a box-aligned extraction fills the rect it was cut from"
    )
    assert padded_w < 0.85 * aligned_w and padded_h < 0.85 * aligned_h, (
        f"padding shrank the art to {padded_w}x{padded_h} inside a {rect[0]}x{rect[1]} rect"
    )


def test_inpaint_mask_covers_children_with_margin():
    mask = boxes_to_mask((100, 100), [(40, 40, 60, 60)])
    assert mask[50, 50] == 255
    assert mask[38, 50] == 255, "boxes are dilated to catch the icon's antialiased rim"
    assert mask[0, 0] == 0


def test_an_occluders_own_silhouette_masks_less_than_its_box():
    """The fix for the rectangular scars. Erasing an occluder's bounding box takes the
    frame's corners with it — five nav icons erased by box cut a staircase of square
    notches out of the top edge of the nav bar sprite. The icons have already been
    extracted by then, so their exact shape is known and only that shape needs to go."""
    sprite = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    ImageDraw.Draw(sprite).ellipse([0, 0, 39, 39], fill=(255, 0, 0, 255))

    box = (30, 30, 70, 70)
    exact = alpha_to_mask((100, 100), sprite, box)
    crude = boxes_to_mask((100, 100), [box], dilate=False)

    assert exact[50, 50] == 255, "the occluder itself must be masked"
    assert exact[31, 31] == 0, "a circle's box corner is frame, and must survive"
    assert crude[31, 31] == 255, (
        "if the box mask spares the corner too, this test is not measuring the difference"
    )
    assert exact.sum() < 0.85 * crude.sum()


def test_deocclusion_leaves_unmasked_pixels_alone():
    """De-occlusion changes what the screenshot shows inside the mask, and nothing else."""
    shot = np.asarray(_screenshot().convert("RGB")).copy()
    h, w = shot.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    mask[140:170, 180:220] = 255
    truth = shot.copy()
    shot[mask > 0] = (255, 40, 40)  # an occluder painted onto the screenshot

    out, method = deocclude(shot, mask, method="telea")
    assert method == "telea"

    assert np.array_equal(truth[mask == 0], out[mask == 0]), (
        "inpainting reached outside its own mask"
    )
    red = np.abs(out.astype(int) - np.array([255, 40, 40])).sum(axis=2) < 60
    assert red.mean() < 0.02, "the occluder survived inpainting"


def test_deocclusion_declines_when_the_mask_swallows_the_picture():
    """With almost nothing left to infer from, every backend invents. Returning the
    occluded original is a downgrade; returning a hallucinated one is a defect."""
    shot = np.asarray(_screenshot().convert("RGB"))
    full = np.full(shot.shape[:2], 255, dtype=np.uint8)
    out, method = deocclude(shot, full, method="telea")
    assert method == "none"
    assert np.array_equal(out, shot)


def test_deoccluding_the_screenshot_leaves_no_rectangular_scar():
    """The nav bar regression, end to end.

    An icon overhanging a bar used to be erased from the *extracted sprite* by bounding
    box, and the frame's silhouette behind it then had to be guessed — which put a
    staircase of square notches along the bar's top edge. De-occluding the screenshot
    first means the bar's real outline is simply visible, so the extraction reads it off
    the picture and the top edge comes back straight.
    """
    w, h = 400, 300
    bar_top, bar_bottom = 120, 180
    img = Image.new("RGB", (w, h), BACKDROP)
    d = ImageDraw.Draw(img)
    d.rectangle([40, bar_top, w - 40, bar_bottom], fill=(58, 132, 220))
    # An icon sitting on the bar and rising well above it — the active-tab arch.
    icon_box = (150, 90, 230, 170)
    d.ellipse(list(icon_box), fill=(240, 200, 60))

    sprite = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
    ImageDraw.Draw(sprite).ellipse([0, 0, 79, 79], fill=(240, 200, 60, 255))
    mask = alpha_to_mask((w, h), sprite, icon_box)

    cleaned, method = deocclude(np.asarray(img), mask, method="telea")
    assert method == "telea"

    bar_pct = (10.0, 100.0 * bar_top / h, 80.0, 100.0 * (bar_bottom - bar_top) / h)
    cut, _, _ = extract_region(Image.fromarray(cleaned), bar_pct)
    alpha = np.asarray(cut)[:, :, 3]

    # The signature of a box-shaped bite is a CLIFF: alpha truncated to a rectangle steps
    # from full height to nothing between two adjacent columns. Measured on this fixture, a
    # bounding-box repair jumps 51 pixels in one column; de-occluding at the source moves
    # by 13 even with Telea doing the filling, which is a soft dip and not a notch.
    #
    # Uniform height is deliberately NOT the assertion. How faithfully the bar's top edge is
    # rebuilt under the icon is the inpainter's business and varies with it — Telea is
    # forced here so the test is deterministic and offline. What must hold whatever fills
    # the hole is that the sprite's outline has no square corners cut into it.
    heights = (alpha > 128).sum(axis=0).astype(int)[5:-5]
    jump = int(np.abs(np.diff(heights)).max())
    assert jump < 0.4 * heights.max(), (
        f"the bar's edge drops {jump} pixels between adjacent columns (of {heights.max()}) — "
        "that is the box-shaped bite de-occluding at the source exists to prevent"
    )


def test_growth_recovers_one_clipped_side_without_inflating_the_others():
    """Sides are grown and verified one at a time. Grown together and judged on the
    combined strip, a tight box on a rectangular element reads as clipped on all four
    sides, three grow into backdrop, and their zero gain averages the one real side below
    threshold — which is how the PLAY button lost its bottom bevel."""
    img = Image.new("RGB", (400, 300), BACKDROP)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = _box_px(img)
    # The panel runs 20px past the right edge of the detected box and nowhere else. It is
    # inset top and bottom so the mask has somewhere to be background: a mask filling its
    # box completely is rejected as degenerate before growth is ever considered.
    d.rounded_rectangle([x0, y0 + 8, x1 + 20, y1 - 9], 14,
                        fill=(58, 132, 220), outline=(214, 176, 62), width=6)

    _, _, grown = extract_region(img, BOX_PCT)
    gx, gy, gw, gh = grown
    assert gx == BOX_PCT[0] and gy == BOX_PCT[1], "no side but the clipped one may move"
    assert gw > BOX_PCT[2] + 2, f"the clipped right side was not recovered ({gw})"
    assert gh <= BOX_PCT[3] + 1, f"an unclipped side was inflated ({gh})"


def test_the_retry_ladder_may_not_cut_inside_the_box_without_solving_the_bleed():
    """The tightened rung of the bleed retry ladder ranks lowest-score-wins, and ranking
    assumes the score is measuring an intruder. `_foreign_blob_frac` on a large composite
    element measures the element's own artwork instead, so "lowest" degenerates to
    "smallest box" — the Matchmaking versus panel scored 0.84 at the rect the user drew and
    0.76 one rung tighter, and came back clipped 10% on every side. Enlarging the region
    just fed a bigger box to the same 10% cut, which is the bug as the user meets it.

    Both scores being nowhere near clean is the tell that nothing was ever eaten. So a box
    that cuts inside the one that was asked for has to come back UNDER the retry threshold,
    not merely under the other candidate.
    """
    from app.processing.extract import _BLEED_RETRY_FRAC, _pick_candidate

    asked, tighter = (0, 0, 100, 100), (5, 5, 95, 95)
    dirty = _BLEED_RETRY_FRAC * 2

    # Both candidates filthy: the tighter one is not a fix, it is a smaller box.
    _, box, _, _ = _pick_candidate([
        (dirty + 0.08, asked, "mask", False),
        (dirty, tighter, "mask", True),
    ])
    assert box == asked, "a tightened box won on score alone without ever getting clean"

    # ...but a tightened box that genuinely resolves the bleed is what the ladder is FOR.
    _, box, _, _ = _pick_candidate([
        (dirty + 0.08, asked, "mask", False),
        (_BLEED_RETRY_FRAC / 2, tighter, "mask", True),
    ])
    assert box == tighter, "a tightened box that cleaned up the mask was rejected"


def test_an_occluder_only_has_to_overlap_a_frame_to_be_lifted_off_it():
    """Requiring containment got the common case wrong: a level badge hangs off the bottom
    corner of a profile banner, a gem overhangs its pill, an active-tab arch rises above
    the nav bar. All three were ~70-80% inside, all three were skipped, and all three
    stayed baked into the frame — which is what "the profile banner has all three elements
    inside it" was describing."""
    from types import SimpleNamespace as NS

    from app.routers.mockups import _occluder_boxes

    banner = NS(id=1, x=0.0, y=0.0, w=40.0, h=10.0)
    avatar = NS(id=2, x=2.0, y=1.0, w=12.0, h=8.0)      # fully inside
    badge = NS(id=3, x=4.0, y=7.0, w=8.0, h=6.0)        # hangs off the bottom
    neighbour = NS(id=4, x=60.0, y=0.0, w=20.0, h=10.0)  # elsewhere
    container = NS(id=5, x=0.0, y=0.0, w=100.0, h=100.0)  # bigger: behind, not on top

    boxes = _occluder_boxes(banner, [banner, avatar, badge, neighbour, container], 1000, 1000)
    assert len(boxes) == 2, f"expected the avatar and the badge, got {boxes}"

    _, _, _, badge_bottom = max(boxes, key=lambda b: b[1])
    assert badge_bottom <= 100, (
        "an occluder's box is clipped to the frame's own crop, so a badge hanging off the "
        "corner erases that corner rather than dragging the mask outside the frame"
    )


def _icon_on_frame(size=(120, 120)):
    """A dark diamond on a vertically-shaded blue frame — an icon drawn on a button. The
    diamond's notches show the frame through them, which is the case both segmenters get
    wrong by absorbing the frame's colour into the object."""
    w, h = size
    ramp = np.linspace(70, 200, h).astype(np.uint8)
    img = np.dstack([np.zeros((h, w), np.uint8),
                     np.tile((ramp * 0.7).astype(np.uint8)[:, None], (1, w)),
                     np.tile(ramp[:, None], (1, w))])
    pic = Image.fromarray(img, "RGB").resize((w * 4, h * 4), Image.NEAREST)
    ImageDraw.Draw(pic).polygon(
        [(w * 2, 56), (w * 4 - 80, h * 2), (w * 2, h * 4 - 56), (80, h * 2)],
        fill=(230, 60, 40),
    )
    # Drawn at 4x and reduced, so the diamond's diagonals carry real antialiasing. PIL's
    # polygon fill is hard-edged, and a fixture with no soft rim cannot exercise a matte.
    return pic.resize((w, h), Image.LANCZOS)


def test_extraction_does_not_keep_the_frame_it_was_lifted_off():
    """A sword cut out of the PLAY button came back with slabs of button behind it: the
    definite-foreground core GrabCut seeds is an inset rectangle, so on a narrow object
    most of that core is frame and the colour model learns the frame's blue. Measured on
    the real screenshot, GrabCut kept the slab at 48% coverage where SAM2 returned the
    sword alone at 29.5%, which is why SAM2 leads and the colour-based corrections that
    used to chase this are gone."""
    shot = _icon_on_frame()
    cut, _, _ = extract_region(shot, (16.0, 16.0, 68.0, 68.0))
    arr = np.asarray(cut)
    opaque = arr[:, :, 3] > 128
    rgb = arr[:, :, :3].astype(int)
    # Anything kept must be the diamond's red, not the frame's blue.
    bluish = opaque & (rgb[:, :, 2] > rgb[:, :, 0] + 40)
    assert bluish.sum() < 0.05 * opaque.sum(), (
        f"{bluish.sum()} of {opaque.sum()} kept pixels are still the frame behind the icon"
    )


def _rect_to_px(size, rect, scale):
    """A percent rect back to pixels the way `extract_region` maps it, at `scale`. The
    sprite it returns is the crop of exactly this rect, and that correspondence is the
    contract every downstream placer relies on."""
    w, h = size[0] * scale, size[1] * scale
    x, y, bw, bh = rect
    x0 = max(0, min(w - 1, int(round(w * x / 100))))
    y0 = max(0, min(h - 1, int(round(h * y / 100))))
    x1 = max(x0 + 1, min(w, int(round(w * (x + bw) / 100))))
    y1 = max(y0 + 1, min(h, int(round(h * (y + bh) / 100))))
    return (x1 - x0, y1 - y0)


def test_supersampled_extraction_returns_a_bigger_sprite_with_the_same_rect():
    """The scale plumbing. Extraction runs on an enlarged screenshot so the boundary has
    pixels to be resolved in, but the rect it reports stays in percent — everything
    downstream places the sprite by that rect, and a sprite four times the size in a rect
    that moved would land misaligned.

    The array is supplied directly rather than through `upres.supersample`, so this tests
    the plumbing without needing torch or a 65MB model.

    Growth is held out of the scale comparison, and that is a statement about growth rather
    than a convenience. Growth re-segments a moved box, and segmenting the enlarged
    screenshot is a genuinely different — better — answer; that is the entire reason
    supersampling exists (see `upres.supersample`). So the two scales agree on WHICH side is
    clipped and disagree on how far to push it: measured on this fixture, native grows the
    top by 5px and x4 grows it by the equivalent of 11. Requiring those to match would be
    requiring supersampling not to change segmentation. With growth off, the two rects agree
    to within 0.2% — and the sprite-to-rect correspondence, which is what actually protects
    the compositor, is asserted below at both scales WITH growth on.
    """
    import cv2

    shot = _icon_on_frame()
    box = (16.0, 16.0, 68.0, 68.0)
    big = cv2.resize(
        np.asarray(shot.convert("RGB")), None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC
    )

    native, _, native_rect = extract_region(shot, box, grow=False)
    scaled, _, scaled_rect = extract_region(shot, box, source=(big, 4), grow=False)

    assert scaled.width == pytest.approx(native.width * 4, abs=4)
    assert scaled.height == pytest.approx(native.height * 4, abs=4)
    for got, want in zip(scaled_rect, native_rect):
        assert got == pytest.approx(want, abs=1.0), (
            "the reported rect must stay in the screenshot's own coordinates"
        )

    # A sprite is exactly the crop of the rect it reports, at the scale it was cut at. This
    # is what breaks if the returned percentages are ever computed against the supersampled
    # dimensions instead of being scale-free, and it holds through growth.
    for grow in (False, True):
        for source, scale in ((None, 1), ((big, 4), 4)):
            sprite, _, rect = extract_region(shot, box, source=source, grow=grow)
            assert sprite.size == _rect_to_px(shot.size, rect, scale), (
                f"sprite {sprite.size} does not match its own rect {rect} "
                f"at scale {scale} (grow={grow})"
            )


def test_edges_are_antialiased_rather_than_stair_stepped():
    """What "crisp" means here. A binary mask gives hard jaggies on a diagonal; a blurred
    mask gives a wide skirt of the old backdrop. Matting gives a narrow band of genuinely
    fractional coverage — so partial alpha must exist, and must stay near the boundary."""
    import cv2

    shot = _icon_on_frame()
    cut, _, _ = extract_region(shot, (16.0, 16.0, 68.0, 68.0))
    alpha = np.asarray(cut)[:, :, 3]

    partial = (alpha > 24) & (alpha < 232)
    solid = alpha >= 232
    assert partial.sum() > 0, "a diagonal edge with no fractional alpha is stair-stepped"

    boundary = cv2.dilate(solid.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=4) > 0
    assert (partial & ~boundary).sum() == 0, (
        "partial alpha away from the boundary is a blur, not a matte"
    )
    assert partial.sum() < 0.5 * solid.sum(), "the soft band is too wide to read as crisp"


def test_matte_removes_the_old_backdrop_from_edge_pixels():
    """Solving for coverage is only half of matting. Left as the blend it was, an edge
    pixel carries the screenshot's own backdrop and fringes the moment the sprite is
    composited anywhere else."""
    shot = _icon_on_frame()
    cut, _, _ = extract_region(shot, (16.0, 16.0, 68.0, 68.0))
    arr = np.asarray(cut).astype(int)
    edge = (arr[:, :, 3] > 96) & (arr[:, :, 3] < 232)
    if not edge.any():
        return
    # Edge pixels should read as the diamond's red, not as a red/blue mixture.
    rgb = arr[:, :, :3][edge]
    assert (rgb[:, 0] > rgb[:, 2]).mean() > 0.8, (
        "edge pixels still carry the blue frame they were cut off"
    )


def test_an_elements_own_interior_survives_matching_the_backdrop():
    """The social nav icon's shield is filled with exactly the blue of the bar it sits on,
    and the button showing between a sword's blade and its guard is exactly that blue too.
    Colour cannot separate them; enclosure can, asked before the morphological close seals
    the notch."""
    w = h = 120
    img = Image.new("RGB", (w, h), (60, 110, 190))
    d = ImageDraw.Draw(img)
    d.ellipse([18, 18, w - 18, h - 18], fill=(220, 180, 60))       # gold ring
    d.ellipse([32, 32, w - 32, h - 32], fill=(60, 110, 190))       # interior: bar's own blue

    cut, _, _ = extract_region(img, (12.0, 12.0, 76.0, 76.0))
    arr = np.asarray(cut)
    cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
    assert arr[cy, cx, 3] > 128, (
        "the element's enclosed interior was deleted for sharing the backdrop's colour"
    )


def test_update_region_resets_detect_rect():
    """Updating a region's coordinates must reset detect_rect so future extractions cut
    from the saved user bounding box rather than reverting to the original proposal."""
    from app.models import MockupRegion
    from app.schemas import RegionUpdate
    from app.routers.mockups import update_region

    region = MockupRegion(
        id=999, mockup_id=1, name="PlayerBannerText",
        x=10.0, y=10.0, w=20.0, h=5.0,
        detect_rect=[10.0, 10.0, 20.0, 5.0],
        color="#6c8cff", prompt="", asset_type="ui_element"
    )

    class DummyDB:
        def get(self, model, id):
            return region
        def commit(self):
            pass

    db = DummyDB()
    update_body = RegionUpdate(x=15.0, y=12.0, w=25.0, h=6.0)
    updated = update_region(999, update_body, db=db)

    assert updated.x == 15.0
    assert updated.y == 12.0
    assert updated.w == 25.0
    assert updated.h == 6.0
    assert updated.detect_rect == [15.0, 12.0, 25.0, 6.0], (
        "detect_rect was not updated; rebuild will extract from the old bounding box"
    )

