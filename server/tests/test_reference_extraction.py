"""Reference mode's "keep only X" path: the prompt it composes and the reference it sends.

Both halves are one mechanism. The extraction wording tells the model that the background
has already been keyed to magenta and one ragged patch was missed, which is only a true
statement about the image if `letterbox_reference` has run on it first — so a change that
breaks either half silently un-does the fix (measured on asset 44: leftover band 15.2% ->
0.3%). These tests pin the contract between them.
"""
from PIL import Image

from app.models import Project
from app.processing.reference import CHROMA, letterbox_reference, snap_ratio
from app.prompting import (
    REFERENCE_BASE,
    REFERENCE_EXTRACT_BASE,
    compose_sections,
    extraction_op_keys,
    reference_instruction,
)
from app.providers.registry import EXTRACTION_MODEL, extraction_model_warning


def test_extraction_op_switches_the_base():
    """A keep-only op must not be composed onto "keep the exact same artwork"."""
    plain = reference_instruction(["upscale", "clean_edges"])
    assert REFERENCE_BASE in plain
    assert "Change ONLY the following" in plain

    extract = reference_instruction(["text_only", "clean_edges", "keep_colors"])
    assert REFERENCE_EXTRACT_BASE in extract
    assert REFERENCE_BASE not in extract
    # The sentence that caused the bug: it promises everything unlisted survives, which is
    # the opposite of what a keep-only op asks for.
    assert "Change ONLY the following" not in extract


def test_extraction_op_is_stated_first():
    """Read the other way round the model has already been told the job is reproduction."""
    instr = reference_instruction(["keep_colors", "clean_edges", "element_only"])
    assert instr.index("(1) Reproduce ONLY the element") < instr.index("Keep exactly the reference's colors")


def test_no_ops_is_unaffected():
    assert reference_instruction([]).startswith(REFERENCE_BASE)


def test_extraction_keys_come_from_the_keep_only_group():
    assert extraction_op_keys() == {"text_only", "element_only"}


def _sections(ops):
    project = Project(id=1, name="p", style_description="Hand-drawn", palette=["#fff"])
    return compose_sections(
        project, "ui_element", "", aspect_ratio="16.68:1", resolution="2048x288",
        prompt_mode="reference", reference_ops=ops,
    )


def test_extraction_drops_the_aspect_instruction():
    """It tells the model to fill the frame with no letterboxing, which is the opposite of
    what an extraction reference is — and the model obeyed it by enlarging a one-line
    caption until it re-wrapped onto two."""
    assert _sections(["text_only", "clean_edges"])["aspect"] == ""
    # Non-extraction reference mode is untouched: nothing there letterboxes anything.
    assert "16.68:1" in _sections(["upscale", "clean_edges"])["aspect"]


def test_snap_ratio_prefers_the_nearest_shape_in_log_space():
    # 16.68:1 against gpt_image_2's menu — the widest entry, not the one that happens to
    # be closest by raw difference.
    assert snap_ratio(2048, 288, ["1:1", "4:3", "3:4", "16:9", "9:16"]) == "16:9"
    # A tall sprite must not be dragged to square more readily than a wide one.
    assert snap_ratio(200, 300, ["1:1", "3:2", "2:3"]) == "2:3"
    assert snap_ratio(300, 200, ["1:1", "3:2", "2:3"]) == "3:2"
    assert snap_ratio(100, 100, []) is None


def test_letterbox_pads_onto_magenta_without_shrinking_the_art(tmp_path):
    src = tmp_path / "ref.png"
    # A wide transparent strip with an opaque red subject, like an extracted caption.
    art = Image.new("RGBA", (1000, 60), (0, 0, 0, 0))
    art.paste((255, 0, 0, 255), (0, 20, 1000, 40))
    art.save(src)

    out = letterbox_reference(src, tmp_path / "out.png", "16:9")
    with Image.open(out) as im:
        canvas = im.convert("RGBA")

    assert canvas.size == (1000, 562)          # grew the short side only
    assert canvas.getpixel((5, 5)) == CHROMA   # padding is the reserved key colour
    # The subject is centred and untouched, not resampled to fit.
    assert canvas.getpixel((500, 281)) == (255, 0, 0, 255)


def test_letterbox_flattens_transparency_even_with_no_ratio(tmp_path):
    """A provider that declares no canvas menu still needs the magenta premise to hold."""
    src = tmp_path / "ref.png"
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(src)

    out = letterbox_reference(src, tmp_path / "out.png", None)
    with Image.open(out) as im:
        canvas = im.convert("RGBA")

    assert canvas.size == (40, 40)
    assert canvas.getpixel((20, 20)) == CHROMA


def test_extraction_model_warning_only_fires_off_the_pin():
    pinned = EXTRACTION_MODEL["higgsfield"]
    assert extraction_model_warning("higgsfield", pinned) is None
    warning = extraction_model_warning("higgsfield", "gpt_image_2")
    assert warning and "gpt_image_2" in warning and pinned in warning
    # A provider with no measured extraction model at all (e.g. antigravity) has nothing
    # to recommend switching to, so it stays silent rather than warning about nothing.
    assert extraction_model_warning("antigravity", "gemini-3.1-pro-low") is None


def test_letterbox_survives_a_malformed_ratio(tmp_path):
    """A bad enum entry is not worth failing a paid generation over."""
    src = tmp_path / "ref.png"
    Image.new("RGBA", (30, 10), (1, 2, 3, 255)).save(src)

    out = letterbox_reference(src, tmp_path / "out.png", "not-a-ratio")
    with Image.open(out) as im:
        assert im.size == (30, 10)
