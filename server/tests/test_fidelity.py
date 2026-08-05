from PIL import Image, ImageDraw

from app.processing.fidelity import (
    fit_mode_for, render_like_compositor, score_asset, score_composite,
)


def _pill(size=(200, 80), fill=(212, 175, 55), outline=(120, 90, 20), bg=None):
    """A rounded 'currency pill' on either a busy background (reference) or alpha."""
    mode = "RGB" if bg else "RGBA"
    img = Image.new(mode, size, bg or (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        [10, 10, size[0] - 10, size[1] - 10], 20, fill=fill, outline=outline, width=4
    )
    return img


def test_exact_extraction_scores_near_perfect():
    """An asset whose pixels came straight out of the reference must score ~100. This is
    the property the whole extraction-over-generation argument rests on."""
    ref = _pill(bg=(70, 130, 180))
    exact = _pill()
    result = score_asset(ref, exact)
    assert result["delta_e"] < 1.0
    assert result["score"] > 95


def test_hue_drift_is_penalised_even_with_perfect_shape():
    """The real-world failure: a regenerated element with the right silhouette but the
    wrong colour. Structure alone must not be able to hide it."""
    ref = _pill(bg=(70, 130, 180))
    drifted = _pill(fill=(150, 190, 140), outline=(90, 110, 60))
    result = score_asset(ref, drifted)
    assert result["ssim"] > 0.9, "shape is identical, so SSIM should stay high"
    assert result["delta_e"] > 10, "colour is clearly wrong"
    assert result["score"] < 70


def test_empty_asset_scores_zero():
    ref = _pill(bg=(70, 130, 180))
    blank = Image.new("RGBA", ref.size, (0, 0, 0, 0))
    result = score_asset(ref, blank)
    assert result["score"] == 0.0
    assert result["coverage"] == 0.0


def test_flat_crop_drops_alpha_term_instead_of_capping_score():
    """A crop with no figure/ground contrast yields no usable foreground estimate. The
    alpha term must be dropped and the rest renormalised, not scored as zero — otherwise
    a perfect match on a flat element is permanently capped below 100."""
    flat = Image.new("RGB", (120, 60), (30, 40, 90))
    same = Image.new("RGBA", (120, 60), (30, 40, 90, 255))
    result = score_asset(flat, same)
    assert result["alpha_iou"] is None
    assert result["score"] == 100.0


def test_score_is_measured_through_the_compositor_fit():
    """A 9-sliced frame is stretched by the compositor before it is seen, so scoring must
    stretch it too. Scored raw, a small frame against a wide box would look wrong."""
    ref = _pill(size=(400, 80), bg=(70, 130, 180))
    small = _pill(size=(120, 80))
    border = {"l": 30, "t": 30, "r": 30, "b": 30}
    sliced = score_asset(ref, small, fit="slice", nine_slice=border)
    stretched = score_asset(ref, small, fit="stretch")
    assert sliced["score"] > stretched["score"]


def test_render_like_compositor_matches_target_box():
    img = _pill(size=(120, 60))
    out = render_like_compositor(img, 300, 90, fit="contain")
    assert out.size == (300, 90)


def test_fit_mode_matches_preview_branches():
    assert fit_mode_for("ui_element", {"l": 4, "t": 4, "r": 4, "b": 4}) == "slice"
    assert fit_mode_for("icon", None) == "contain"
    assert fit_mode_for("ui_element", None) == "stretch"
    # tiles are never sliced even when a border is present
    assert fit_mode_for("tile", {"l": 4, "t": 4, "r": 4, "b": 4}) == "stretch"


def test_composite_score_discounts_unfilled_area():
    """Half a screen rebuilt perfectly must not score the same as a whole one. Quality
    stays high; the headline score is discounted by coverage."""
    ref = Image.new("RGB", (200, 200), (200, 60, 60))
    half = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    half.paste(Image.new("RGBA", (200, 100), (200, 60, 60, 255)), (0, 0))

    result = score_composite(ref, half)
    assert 0.45 < result["filled"] < 0.55
    assert result["quality"] > 95, "the part that was drawn is exact"
    assert 40 < result["score"] < 55, "headline is discounted by coverage"


def test_composite_score_ignores_backdrop_colour_behind_holes():
    """Regression: the screen score used to flatten the composite onto a grey backdrop,
    so an empty screen was scored as 'grey vs. game art' — an arbitrary number that moved
    when the preview's checkerboard changed. Holes must not contribute colour error."""
    ref = Image.new("RGB", (100, 100), (10, 200, 40))
    empty = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    result = score_composite(ref, empty)
    assert result["filled"] == 0.0
    assert result["score"] == 0.0


def test_uncut_rectangle_scores_below_a_properly_cut_sprite():
    """The defining test for extraction quality. An uncut rectangle *is* the reference
    crop, so colour and structure both say 'perfect' — for a long while the metric
    ranked it level with a correctly cut sprite, which made it blind to the one failure
    mode extraction actually has (mask covers the whole box, background comes along for
    the ride). `bg_bleed` is what separates them: opaque pixels whose colour matches the
    surrounding backdrop are counted as leaked background."""
    backdrop = (30, 40, 90)
    reference = _pill(bg=backdrop)

    cut = _pill()                                        # alpha outside the pill
    uncut = reference.convert("RGBA")                    # the crop, verbatim, opaque

    kw = dict(fit="stretch", background_rgb=backdrop)
    cut_score = score_asset(reference, cut, **kw)
    uncut_score = score_asset(reference, uncut, **kw)

    assert cut_score["bg_bleed"] < 0.05, "a cut sprite leaks almost no backdrop"
    assert uncut_score["bg_bleed"] > 0.25, "an uncut rectangle is mostly backdrop"
    assert cut_score["score"] > uncut_score["score"] + 5


def test_bg_bleed_is_skipped_when_the_backdrop_is_unknown():
    """Scoring must still work without a backdrop sample (region flush to the image
    edge). The term drops out and the remaining weights renormalise, rather than the
    asset being charged for something that could not be measured."""
    reference = _pill(bg=(30, 40, 90))
    result = score_asset(reference, _pill(), fit="stretch")
    assert result["bg_bleed"] is None
    assert result["score"] > 90


def test_screen_score_is_normalised_to_the_area_in_scope():
    """A perfect reconstruction of everything detected scored 18/100 because the backdrop
    and the hero — which nothing had been asked to produce yet — are four fifths of the
    canvas. Scoped to the union of the region rects the headline answers the question
    actually being asked, and `screen_filled` keeps the other one visible instead of
    folding the two into one unreadable figure."""
    import numpy as np

    reference = Image.new("RGB", (200, 200), (40, 60, 120))
    ImageDraw.Draw(reference).rectangle([20, 20, 99, 99], fill=(220, 180, 60))

    # The pipeline reproduced its one region exactly and nothing else.
    composite = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(composite).rectangle([20, 20, 99, 99], fill=(220, 180, 60, 255))

    target = np.zeros((200, 200), bool)
    target[20:100, 20:100] = True

    whole = score_composite(reference, composite)
    scoped = score_composite(reference, composite, target=target)

    assert scoped["quality"] == whole["quality"], "scope must not change what was measured"
    assert scoped["filled"] > 0.99, "the area in scope was fully rebuilt"
    assert scoped["score"] > 95, f"a complete in-scope rebuild should read as such: {scoped}"
    assert whole["score"] < 25, "the unscoped number is dominated by what nobody asked for"
    assert scoped["screen_filled"] == whole["filled"], (
        "the honest whole-canvas coverage is still reported"
    )
