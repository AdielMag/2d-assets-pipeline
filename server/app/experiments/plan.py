"""What a sweep is made of: arms (a config to test) and jobs (one provider call each).

A `Job` is deliberately plain data with absolute file paths and no ORM objects. The runner
builds every job up front, closes the database session, and only then starts calling the
provider — so a long sweep holds no session open, and no code path exists through which a
provider call could write to the catalogue it is measuring against.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from ..models import Asset, Mockup
from ..processing.fidelity import fit_mode_for
from ..scoring import region_crop_image, surrounding_color
from ..storage import abs_path

# The ops each step actually ticks, copied from their call sites in routers.mockups so a
# sweep measures the real thing: `_polish_regions` (base_ops) and `_redraw_built_regions`
# (remove_text for the parent, text_only+upscale per label).
POLISH_OPS = ["upscale", "clean_edges", "keep_colors"]
TEXT_REMOVE_OPS = ["remove_text"]
TEXT_ISOLATE_OPS = ["text_only", "upscale"]


@dataclass
class Arm:
    """One configuration under test: a model, its per-model params, and a prompt wording."""
    model: str
    params: dict | None = None
    prompt_variant: str = "v1"
    label: str | None = None

    @property
    def key(self) -> str:
        """Filesystem-safe identity. Includes params and variant because the same model at
        two resolutions is two different arms with two different prices."""
        if self.label:
            return re.sub(r"[^A-Za-z0-9_.-]+", "-", self.label)
        bits = [self.model]
        for k in sorted(self.params or {}):
            bits.append(f"{k}={self.params[k]}")
        if self.prompt_variant != "v1":
            bits.append(self.prompt_variant)
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", "__".join(bits))

    def describe(self) -> str:
        parts = [self.model]
        if self.params:
            parts.append(" ".join(f"--{k} {v}" for k, v in sorted(self.params.items())))
        if self.prompt_variant != "v1":
            parts.append(f"prompt={self.prompt_variant}")
        return "  ".join(parts)


@dataclass
class Job:
    """One provider call, plus everything needed to grade its result without the database.

    `ref_path` is what the model is shown; `truth_path` is what the answer is graded
    against. For polish they are different images of the same thing — the model sees the
    built sprite, but is graded against the original screenshot pixels, because "looks like
    the sprite it was given" is not the question. The question is whether it still looks
    like what was on screen.
    """
    key: str
    kind: str                      # polish | text_remove | text_isolate
    ops: list[str]
    ref_path: str                  # absolute; the image handed to the model
    truth_path: str                # absolute; the ground-truth crop it is graded against
    ref_ratio: str                 # w:h of ref_path, for trim_for_fit after generation
    sliced: bool
    fit: str
    nine_slice: dict | None = None
    background_rgb: list[float] | None = None
    # text_remove: label boxes within the truth crop, so residue can be measured where the
    # lettering used to be. text_isolate: the caption the sprite is supposed to contain.
    label_boxes: list[list[int]] = field(default_factory=list)
    label_text: str | None = None
    region_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _ratio(width: int, height: int) -> str:
    from ..prompting import ratio_string

    return ratio_string(width, height)


def build_jobs(
    db, mockup_id: int, out_dir: Path,
    polish_regions: list[str], text_regions: list[str], isolate_labels: list[tuple[str, str]],
) -> list[Job]:
    """Assemble the fixed panel of jobs every arm will run, writing ground-truth crops to
    `out_dir` so grading never needs the database again.

    `isolate_labels` is (region name, caption) pairs — the caption is matched on its text
    rather than its index because label ids shift whenever detection re-runs.
    """
    from ..routers.mockups import _label_items_within, _mockup_size, _pad_box

    mockup = db.get(Mockup, mockup_id)
    if mockup is None:
        raise ValueError(f"no mockup {mockup_id}")
    W, H = _mockup_size(mockup)
    truth_dir = out_dir / "_truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    by_name = {r.name: r for r in mockup.regions}
    screenshot = abs_path(mockup.image_path)

    jobs: list[Job] = []

    def _built_sprite(region) -> tuple[Path, Asset] | None:
        """The region's currently-selected asset image — the same starting point
        `_redraw_built_regions` hands the model."""
        if region is None or region.asset_id is None:
            return None
        asset = db.get(Asset, region.asset_id)
        version = asset.selected_version if asset else None
        if version is None or not version.processed_path:
            return None
        path = abs_path(version.processed_path)
        return (path, asset) if path.exists() else None

    def _truth_for(region, name: str) -> tuple[str, list[float] | None] | None:
        crop = region_crop_image(mockup, region)
        if crop is None:
            return None
        dest = truth_dir / f"{name}.png"
        crop.save(dest)
        box = (
            int(W * region.x / 100), int(H * region.y / 100),
            int(W * (region.x + region.w) / 100), int(H * (region.y + region.h) / 100),
        )
        bg = None
        if screenshot.exists():
            with Image.open(screenshot) as full:
                got = surrounding_color(full.convert("RGB"), box)
            bg = list(got) if got is not None else None
        return str(dest), bg

    # --- Polish: redraw the built sprite, graded against the screenshot pixels ---
    for name in polish_regions:
        region = by_name.get(name)
        built = _built_sprite(region)
        truth = _truth_for(region, f"polish-{name}") if region is not None else None
        if built is None or truth is None:
            continue
        ref_path, asset = built
        with Image.open(ref_path) as im:
            ratio = _ratio(*im.size)
        jobs.append(Job(
            key=f"polish__{name}", kind="polish", ops=list(POLISH_OPS),
            ref_path=str(ref_path), truth_path=truth[0], ref_ratio=ratio,
            sliced=asset.nine_slice is not None,
            fit=fit_mode_for(asset.type, asset.nine_slice), nine_slice=asset.nine_slice,
            background_rgb=truth[1], region_name=name,
        ))

    # --- Text step, part 1: erase the lettering off the parent ---
    for name in text_regions:
        region = by_name.get(name)
        built = _built_sprite(region)
        truth = _truth_for(region, f"textremove-{name}") if region is not None else None
        if built is None or truth is None:
            continue
        ref_path, asset = built
        with Image.open(ref_path) as im:
            ratio = _ratio(*im.size)
        jobs.append(Job(
            key=f"text_remove__{name}", kind="text_remove", ops=list(TEXT_REMOVE_OPS),
            ref_path=str(ref_path), truth_path=truth[0], ref_ratio=ratio,
            sliced=asset.nine_slice is not None,
            fit=fit_mode_for(asset.type, asset.nine_slice), nine_slice=asset.nine_slice,
            background_rgb=truth[1],
            label_boxes=[list(b) for _lbl, b in _label_items_within(mockup, region, W, H)],
            region_name=name,
        ))

    # --- Text step, part 2: isolate one caption as its own sprite ---
    for name, caption in isolate_labels:
        region = by_name.get(name)
        if region is None or not screenshot.exists():
            continue
        items = [(l, b) for l, b in _label_items_within(mockup, region, W, H)
                 if (l.text or "").strip() == caption]
        if not items:
            continue
        label, box = items[0]
        region_box = (
            int(W * region.x / 100.0), int(H * region.y / 100.0),
            int(W * (region.x + region.w) / 100.0), int(H * (region.y + region.h) / 100.0),
        )
        gx0, gy0 = region_box[0], region_box[1]
        screen_box = (gx0 + box[0], gy0 + box[1], gx0 + box[2], gy0 + box[3])
        # The model gets the padded neighbourhood (identical to `_redraw_built_regions`, so
        # it has context for what the lettering sits on) but is graded against the caption's
        # own tight box — padding is an input aid, not part of the expected output.
        local_box = _pad_box(screen_box, frac=0.4, clip=region_box)
        safe = re.sub(r"[^A-Za-z0-9]+", "", caption) or label.name or "label"
        with Image.open(screenshot) as full:
            rgb = full.convert("RGB")
            ref_dest = truth_dir / f"isolate-{name}-{safe}-ref.png"
            rgb.crop(local_box).save(ref_dest)
            truth_dest = truth_dir / f"isolate-{name}-{safe}.png"
            rgb.crop(screen_box).save(truth_dest)
        jobs.append(Job(
            key=f"text_isolate__{name}__{safe}", kind="text_isolate", ops=list(TEXT_ISOLATE_OPS),
            ref_path=str(ref_dest), truth_path=str(truth_dest),
            ref_ratio=_ratio(screen_box[2] - screen_box[0], screen_box[3] - screen_box[1]),
            sliced=False, fit="contain", label_text=caption, region_name=name,
        ))

    return jobs


def save_jobs(jobs: list[Job], path: Path) -> None:
    path.write_text(json.dumps([j.to_dict() for j in jobs], indent=2), encoding="utf-8")


def load_jobs(path: Path) -> list[Job]:
    return [Job(**d) for d in json.loads(path.read_text(encoding="utf-8"))]
