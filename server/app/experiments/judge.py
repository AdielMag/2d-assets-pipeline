"""Vision LLM grading against a fixed rubric — the axis pixel metrics cannot cover.

Two things here are invisible to `grade.py`. First, *what kind* of wrong an output is: a
redraw that invents a header stripe and one that shifts a gradient can score alike on
CIEDE2000 while only one is a real defect. Second, whether isolated lettering says the
right word — the pipeline has no OCR (no pytesseract, no tesseract binary) and a vision
model reads the sprite directly, so no new dependency is needed for it.

Each arm is judged `SAMPLES` times and the **median** is kept. A single sample from a
sampling model is noise at this granularity; the median is what makes two arms' judge
scores comparable at all.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from ..llm.runner import LlmError, LlmOptions, get_runner

SAMPLES = 3

# Criteria are per-kind because "is the lettering gone" is meaningless for a polish job and
# "did it preserve the text" is meaningless for one that was asked to erase it.
CRITERIA = {
    "polish": ["identity", "edges", "nothing_invented"],
    "text_remove": ["identity", "text_gone", "fill_clean", "nothing_invented"],
    "text_isolate": ["letters_correct", "letters_styled", "background_empty"],
}

_RUBRIC = """You are grading one step of a 2D game-asset pipeline. Score strictly: 5 means
a professional would ship it unchanged, 3 means usable with rework, 1 means unusable.

BEFORE (what the model was given): {ref}
AFTER (what the model produced, transparent background shown as checkerboard): {out}
GROUND TRUTH (the original screenshot pixels this element came from): {truth}

The task given to the model was: {task}

Score each criterion 1-5:
{criteria}

Reply with ONLY a JSON object, no prose: {{{schema}}}"""

_DESCRIPTIONS = {
    "identity": "identity — is it unmistakably the SAME element as the ground truth (same shape, proportions, colours, decoration)? Any redesign, added ornament, changed border or invented panel scores 1-2.",
    "edges": "edges — are edges crisp and clean, free of blur, halo, speckles or leftover compositing noise?",
    "nothing_invented": "nothing_invented — did it avoid adding ANY element, stripe, shadow, badge or detail that is not in the ground truth?",
    "text_gone": "text_gone — is every letter, numeral and label completely gone, with no ghost, smudge or fragment remaining?",
    "fill_clean": "fill_clean — where text used to be, does the surface continue naturally (same colour/gradient/texture), with no plate, patch or seam?",
    "letters_correct": "letters_correct — does the sprite show EXACTLY the characters '{label}' — same spelling, same capitalisation, nothing added or dropped?",
    "letters_styled": "letters_styled — same font weight, colour, outline, gradient and shadow as the ground truth?",
    "background_empty": "background_empty — is everything except the lettering fully transparent, with no button, frame, plate or glow drawn behind it?",
}


def _prompt(job, out_image: Path) -> str:
    keys = CRITERIA[job.kind]
    lines = "\n".join(
        f"- {_DESCRIPTIONS[k].format(label=job.label_text or '')}" for k in keys
    )
    schema = ", ".join(f'"{k}": <1-5>' for k in keys)
    return _RUBRIC.format(
        ref=Path(job.ref_path).resolve(), out=out_image.resolve(),
        truth=Path(job.truth_path).resolve(), task=" + ".join(job.ops),
        criteria=lines, schema=schema,
    )


def _parse(text: str, keys: list[str]) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    out = {}
    for k in keys:
        v = data.get(k)
        if isinstance(v, (int, float)) and 1 <= v <= 5:
            out[k] = float(v)
    return out or None


def judge_job(job, out_image: Path, provider: str = "claude", model: str = "",
              samples: int = SAMPLES) -> dict | None:
    """Median rubric scores for one result, or None if the judge could not be reached.

    Never raises: the judge is a second opinion on a sweep that already has objective
    numbers, so a CLI hiccup must not discard a whole arm's measured results."""
    keys = CRITERIA.get(job.kind)
    if not keys:
        return None
    runner = get_runner(provider)
    if not runner.available():
        return None
    prompt = _prompt(job, out_image)
    options = LlmOptions(
        model=model, effort="low",
        images=[Path(job.ref_path), out_image, Path(job.truth_path)],
    )
    got: list[dict] = []
    for _ in range(samples):
        try:
            parsed = _parse(runner.run(prompt, options), keys)
        except LlmError:
            continue
        if parsed:
            got.append(parsed)
    if not got:
        return None
    scores = {
        k: round(statistics.median([g[k] for g in got if k in g]), 2)
        for k in keys if any(k in g for g in got)
    }
    if not scores:
        return None
    scores["judge_mean"] = round(statistics.fmean(scores.values()), 2)
    scores["samples"] = len(got)
    return scores


def judge_report(report: dict, jobs_by_key: dict, provider: str = "claude",
                 model: str = "", samples: int = SAMPLES, on_event=None) -> dict:
    """Attach rubric scores to every successful result in a finished sweep report."""
    say = on_event or (lambda *a, **k: None)
    for arm in report.get("arms", []):
        for rec in arm["results"]:
            if not rec.get("ok") or not rec.get("image"):
                continue
            job = jobs_by_key.get(rec["job"])
            if job is None:
                continue
            scores = judge_job(job, Path(rec["image"]), provider, model, samples)
            if scores:
                rec.setdefault("grade", {})["judge"] = scores
            say(arm["arm"], rec["job"], scores)
    return report
