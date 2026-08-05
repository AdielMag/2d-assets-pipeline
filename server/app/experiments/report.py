"""Turns a sweep's raw results into a decision: leaderboard, blind sheets, ranking merge.

Two numbers are reported side by side and neither is allowed to win alone. `grade` says how
good the output is; `per_credit` says what that quality costs. A model that scores 3 points
higher for triple the price is a worse default for a pipeline that runs this step across
every element of every screen, and a table that only sorted by grade would hide that.
"""
from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from PIL import Image, ImageDraw

MAGENTA = (255, 0, 255, 255)

# Kinds are averaged separately before being averaged together, so an arm is not rewarded
# for being good at the step that happens to have more jobs in the panel.
KINDS = ("polish", "text_remove", "text_isolate")


def summarize_arm(arm: dict) -> dict:
    by_kind: dict[str, list[float]] = {k: [] for k in KINDS}
    chroma_fail, errors, halos, residues, gains = [], [], [], [], []
    for r in arm["results"]:
        if not r.get("ok"):
            errors.append(r.get("error", "failed"))
            continue
        g = r.get("grade") or {}
        if g.get("headline") is not None:
            by_kind.setdefault(r["kind"], []).append(g["headline"])
        if g.get("chroma_ok") is False:
            chroma_fail.append(r["job"])
        if g.get("halo") is not None:
            halos.append(g["halo"])
        if g.get("magenta_residue") is not None:
            residues.append(g["magenta_residue"])
        if g.get("sharpness_gain") is not None:
            gains.append(g["sharpness_gain"])

    kind_means = {k: (round(statistics.fmean(v), 2) if v else None) for k, v in by_kind.items()}
    measured = [v for v in kind_means.values() if v is not None]
    grade = round(statistics.fmean(measured), 2) if measured else None
    credits = arm.get("credits") or 0.0
    calls = len([r for r in arm["results"] if r.get("ok")])
    return {
        "arm": arm["arm"],
        "describe": arm.get("describe", arm["arm"]),
        "model": arm.get("model"),
        "prompt_variant": arm.get("prompt_variant", "v1"),
        "grade": grade,
        **{f"grade_{k}": kind_means.get(k) for k in KINDS},
        "sharpness_gain": round(statistics.fmean(gains), 3) if gains else None,
        "halo": round(statistics.fmean(halos), 4) if halos else None,
        "residue": round(statistics.fmean(residues), 4) if residues else None,
        "credits": round(credits, 3),
        "per_call": round(credits / calls, 3) if calls else None,
        # Quality bought per credit — the number that decides the default, once the
        # chroma gate has already excluded anything unusable.
        "per_credit": round(grade / credits, 2) if grade and credits else None,
        "chroma_fail": chroma_fail,
        "errors": errors,
        "ok_calls": calls,
        "halted": arm.get("halted"),
    }


def leaderboard(report: dict) -> list[dict]:
    rows = [summarize_arm(a) for a in report.get("arms", [])]
    rows.sort(key=lambda r: (r["grade"] is None, -(r["grade"] or 0)))
    return rows


def print_leaderboard(report: dict, sort: str = "grade") -> list[dict]:
    rows = leaderboard(report)
    if sort == "value":
        rows.sort(key=lambda r: (r["per_credit"] is None, -(r["per_credit"] or 0)))

    head = (f"{'arm':38} {'grade':>6} {'polish':>7} {'txt-rm':>7} {'txt-iso':>7} "
            f"{'sharp':>6} {'cr/call':>8} {'/credit':>8}  flags")
    print(head)
    print("-" * len(head))
    for r in rows:
        def f(v, spec=".2f", w=7):
            return ("—" if v is None else format(v, spec)).rjust(w)
        flags = []
        if r["chroma_fail"]:
            flags.append(f"CHROMA-FAIL x{len(r['chroma_fail'])}")
        if r["errors"]:
            flags.append(f"ERR x{len(r['errors'])}")
        if r["halted"]:
            flags.append("HALTED")
        print(
            f"{r['describe'][:38]:38} {f(r['grade'], '.2f', 6)} {f(r['grade_polish'])} "
            f"{f(r['grade_text_remove'])} {f(r['grade_text_isolate'])} "
            f"{f(r['sharpness_gain'], '.2f', 6)} {f(r['per_call'], '.3f', 8)} "
            f"{f(r['per_credit'], '.1f', 8)}  {', '.join(flags)}"
        )
    print("-" * len(head))
    print(f"total spend: {report.get('credits', 0)} credits over "
          f"{sum(r['ok_calls'] for r in rows)} successful calls")
    return rows


def blind_sheet(
    report: dict, job_key: str, out: Path, cell: int = 340, cols: int = 4, seed: int | None = None
) -> Path:
    """One job, every arm, shuffled and labelled A/B/C — with the key written beside it.

    Composited on magenta for the same reason `tools/contact_sheet.py` is: magenta appears
    nowhere in this artwork, so any pink is a hole and any pink fringe is a bad edge. Arm
    names are withheld from the sheet so a ranking cannot be anchored by brand.
    """
    entries = []
    for arm in report.get("arms", []):
        rec = next((r for r in arm["results"] if r["job"] == job_key and r.get("ok")), None)
        if rec and Path(rec["image"]).exists():
            entries.append((arm["describe"], rec))
    if not entries:
        raise ValueError(f"no successful results for job {job_key}")

    rng = random.Random(seed)
    rng.shuffle(entries)
    letters = [chr(ord("A") + i) for i in range(len(entries))]

    tiles = []
    for letter, (_desc, rec) in zip(letters, entries):
        with Image.open(rec["image"]) as im:
            sprite = im.convert("RGBA").copy()
        sprite.thumbnail((cell, cell - 20), Image.LANCZOS)
        tile = Image.new("RGBA", (cell, cell), (24, 24, 28, 255))
        plate = Image.new("RGBA", sprite.size, MAGENTA)
        plate.alpha_composite(sprite)
        tile.paste(plate, ((cell - sprite.width) // 2, 20))
        ImageDraw.Draw(tile).text((6, 5), letter, fill=(245, 245, 250, 255))
        tiles.append(tile)

    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (24, 24, 28))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * cell, (i // cols) * cell))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)

    key_path = out.with_suffix(".key.json")
    key_path.write_text(json.dumps(
        {"job": job_key, "key": {l: d for l, (d, _) in zip(letters, entries)}}, indent=2
    ), encoding="utf-8")
    return key_path


def apply_ranking(report: dict, job_key: str, key_path: Path, ordering: list[str]) -> dict:
    """Fold a blind ranking back into the report as points (best gets the most).

    Stored rather than blended into `grade`: a human ranking and a pixel metric answer
    different questions, and averaging them would hide a disagreement between the two —
    which is the single most interesting thing a sweep can turn up.
    """
    key = json.loads(key_path.read_text(encoding="utf-8"))["key"]
    n = len(ordering)
    points = {key[letter]: n - i for i, letter in enumerate(ordering) if letter in key}
    for arm in report.get("arms", []):
        arm.setdefault("blind_rank", {})[job_key] = points.get(arm["describe"])
    return report
