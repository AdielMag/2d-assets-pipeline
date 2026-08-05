"""Calibration for the sweep grader.

A grader that cannot separate a right answer from a wrong one cannot rank models, so these
tests check the separation directly rather than asserting on specific numbers: a job graded
against its own ground truth must score near the top, the same job graded against obviously
wrong output must score far below it, and each failure mode the sweep exists to catch must
move its own metric.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.experiments.grade import (  # noqa: E402
    _chroma_health, _halo, _magenta_residue, _sharpness, grade,
)
from app.experiments.plan import Job  # noqa: E402

W, H = 160, 96


def _button(color=(40, 90, 200), text=True) -> Image.Image:
    img = Image.new("RGB", (W, H), (18, 22, 30))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, W - 8, H - 8), radius=14, fill=color, outline=(230, 200, 90), width=3)
    if text:
        d.text((W // 2 - 16, H // 2 - 6), "PLAY", fill=(255, 255, 255))
    return img


def _sprite(img: Image.Image) -> Image.Image:
    """The button as a cut sprite: opaque inside the rounded rect, transparent outside."""
    rgba = img.convert("RGBA")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, W - 8, H - 8), radius=14, fill=255)
    rgba.putalpha(mask)
    return rgba


@pytest.fixture
def truth(tmp_path) -> Path:
    p = tmp_path / "truth.png"
    _button().save(p)
    return p


def _job(tmp_path, truth: Path, kind="polish", **kw) -> Job:
    ref = tmp_path / "ref.png"
    _sprite(_button()).save(ref)
    return Job(
        key="j", kind=kind, ops=["upscale"], ref_path=str(ref), truth_path=str(truth),
        ref_ratio="5:3", sliced=False, fit="stretch", region_name="PlayButton", **kw
    )


def test_correct_output_scores_far_above_wrong_output(tmp_path, truth):
    """The headline separation the whole sweep depends on."""
    job = _job(tmp_path, truth)
    right = grade(job, _sprite(_button()), None)["headline"]
    # Same silhouette, completely different colour — the "model redesigned it" failure.
    wrong = grade(job, _sprite(_button(color=(220, 60, 40))), None)["headline"]
    assert right > 90, f"a faithful reproduction scored only {right}"
    assert right - wrong > 20, f"grader barely separates right ({right}) from wrong ({wrong})"


def test_blank_output_scores_zero(tmp_path, truth):
    job = _job(tmp_path, truth)
    blank = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    assert grade(job, blank, None)["headline"] == 0.0


def test_chroma_gate_detects_missing_key(tmp_path, truth):
    """A model that returns art on its own backdrop instead of a magenta field is unusable,
    and the gate has to say so before any quality metric flatters it."""
    keyed = Image.new("RGB", (W, H), (255, 0, 255))
    keyed.paste(_button(), (20, 20), _sprite(_button()).split()[3].crop((0, 0, W, H)))
    assert _chroma_health(keyed)[1] is True
    assert _chroma_health(_button())[1] is False


def test_magenta_residue_flags_unstripped_fringe(tmp_path):
    clean = _sprite(_button())
    fringed = clean.copy()
    ImageDraw.Draw(fringed).rounded_rectangle((8, 8, W - 8, H - 8), radius=14,
                                              outline=(255, 0, 255), width=3)
    assert _magenta_residue(fringed) > _magenta_residue(clean) + 0.05


def test_halo_flags_soft_edges(tmp_path):
    crisp = _sprite(_button())
    soft = crisp.copy()
    alpha = soft.split()[3].point(lambda v: 128 if v > 0 else 0)
    soft.putalpha(alpha)
    assert _halo(soft) > 0.9
    assert _halo(crisp) < 0.05


def test_sharpness_separates_blur_from_detail(tmp_path):
    from PIL import ImageFilter

    crisp = _sprite(_button())
    blurred = crisp.filter(ImageFilter.GaussianBlur(2.5))
    assert _sharpness(crisp, (W, H)) > _sharpness(blurred, (W, H)) * 2


def test_text_remove_separates_erased_from_intact_by_a_usable_margin(tmp_path):
    """Erasing the text must beat leaving it by enough to rank models on.

    Uses a caption sized like a real one (filling its label box) rather than the tiny
    default-font text elsewhere in this file: residue is a ratio of variation inside the box
    to outside it, so a caption occupying a tenth of its box understates the metric and
    would let this test pass on a grader too blunt to use.
    """
    from PIL import ImageFont

    bw, bh = 320, 160

    def button(text: str | None) -> Image.Image:
        img = Image.new("RGB", (bw, bh), (18, 22, 30))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((10, 10, bw - 10, bh - 10), radius=22,
                            fill=(40, 90, 200), outline=(230, 200, 90), width=5)
        if text:
            d.text((bw // 2, bh // 2), text, fill=(255, 255, 255), anchor="mm", font=_font())
        return img

    def _font():
        # The caption has to actually fill its label box — see the docstring. Arial only
        # exists on Windows, so a bare truetype("arialbd.ttf") fell through to
        # `load_default()` on Linux CI, which is an ~11px bitmap font. That drew "PLAY" at a
        # fraction of the box, which understates residue exactly the way this test is
        # written to catch, and the margin collapsed to 0.05 against a required 0.1.
        for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
            try:
                return ImageFont.truetype(name, 54)
            except OSError:
                continue
        # Pillow >= 10.1 scales its built-in font, so this last resort is still 54px.
        return ImageFont.load_default(size=54)

    def cut(img: Image.Image) -> Image.Image:
        rgba = img.convert("RGBA")
        m = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(m).rounded_rectangle((10, 10, bw - 10, bh - 10), radius=22, fill=255)
        rgba.putalpha(m)
        return rgba

    tp = tmp_path / "t.png"
    button("PLAY").save(tp)
    rp = tmp_path / "r.png"
    cut(button("PLAY")).save(rp)
    bb = ImageDraw.Draw(Image.new("RGB", (bw, bh))).textbbox(
        (bw // 2, bh // 2), "PLAY", anchor="mm", font=_font()
    )
    job = Job(key="j", kind="text_remove", ops=["remove_text"], ref_path=str(rp),
              truth_path=str(tp), ref_ratio="2:1", sliced=False, fit="stretch",
              label_boxes=[[bb[0] - 4, bb[1] - 4, bb[2] + 4, bb[3] + 4]])

    erased = grade(job, cut(button(None)), None)
    kept = grade(job, cut(button("PLAY")), None)
    assert erased["residue"] == 0.0
    assert kept["residue"] > 0.1
    assert erased["headline"] - kept["headline"] > 8, (
        f"only {erased['headline'] - kept['headline']:.2f} points separate a cleanly erased "
        f"caption from an untouched one — too close to rank models on"
    )


def test_text_isolate_rewards_lettering_only(tmp_path):
    """An isolated caption is graded on mask agreement, not colour — a sprite that also
    draws the button must lose to one that draws only the letters."""
    caption = Image.new("RGB", (60, 24), (40, 90, 200))
    ImageDraw.Draw(caption).text((6, 6), "PLAY", fill=(255, 255, 255))
    tp = tmp_path / "cap.png"
    caption.save(tp)
    job = Job(key="j", kind="text_isolate", ops=["text_only"], ref_path=str(tp),
              truth_path=str(tp), ref_ratio="5:2", sliced=False, fit="contain",
              label_text="PLAY")

    letters = Image.new("RGBA", (60, 24), (0, 0, 0, 0))
    ImageDraw.Draw(letters).text((6, 6), "PLAY", fill=(255, 255, 255, 255))
    everything = Image.new("RGBA", (60, 24), (40, 90, 200, 255))
    ImageDraw.Draw(everything).text((6, 6), "PLAY", fill=(255, 255, 255, 255))

    good = grade(job, letters, None)
    bad = grade(job, everything, None)
    assert good["headline"] > bad["headline"], (
        f"lettering-only ({good['headline']}) should beat lettering+backing ({bad['headline']})"
    )
    assert bad["spill"] > good["spill"]
