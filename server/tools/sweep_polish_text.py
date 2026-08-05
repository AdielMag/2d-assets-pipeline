"""Model + prompt bake-off for the screen pipeline's Text and Polish steps.

Both steps call one image model through `_llm_reference_pass`, and the model, its per-model
params and the prompt wording were all previously fixed at whatever the default was. This
runs the same fixed panel of work across several of each and grades the results, so those
choices can be made from evidence instead of taste.

    # what would it cost? (creates no jobs, spends nothing)
    python tools/sweep_polish_text.py plan --stage 0 --mockup 8

    # run it, with a hard credit ceiling
    python tools/sweep_polish_text.py run --stage 0 --mockup 8 --budget 20

    # rank the survivors, then the prompt variants on the winners
    python tools/sweep_polish_text.py run --stage 1 --mockup 8 --models a,b,c --budget 60
    python tools/sweep_polish_text.py run --stage 2 --mockup 8 --models a,b --budget 40

    # read the results
    python tools/sweep_polish_text.py report --run runs/stage1 --sort value
    python tools/sweep_polish_text.py judge  --run runs/stage1
    python tools/sweep_polish_text.py sheet  --run runs/stage1 --job polish__PlayButton
    python tools/sweep_polish_text.py rank   --run runs/stage1 --job polish__PlayButton --order C,A,D,B

Nothing here writes to the application database. Every output lands under
`storage/experiments/<run>/`; the catalogue the sweep measures against is read-only to it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import STORAGE_DIR  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.experiments import judge as judge_mod  # noqa: E402
from app.experiments import report as report_mod  # noqa: E402
from app.experiments.plan import Arm, build_jobs, load_jobs, save_jobs  # noqa: E402
from app.experiments.runner import Budget, dry_run, run_sweep  # noqa: E402

EXPERIMENTS_DIR = Path(STORAGE_DIR) / "experiments"

# The six candidates, each at its cheapest sane config. Prices verified live against
# `higgsfield generate cost`; they are a property of the model+params pair, which is why
# params are part of an arm's identity rather than a global setting.
#
# `aspect_ratio: auto` is on every arm and is not optional. Measured on the first stage-0
# run without it: all six models returned a SQUARE canvas (1024x1024 through 4096x4096) for
# a 2.14:1 button, drawing it small and centred with margin around it. `trim_for_fit` then
# has to crop to content and pad back, and the recovered proportions no longer match — SSIM
# fell from 0.89 to 0.31-0.52 and every model scored 20-45 points BELOW the unpolished
# sprite. That was measuring each model's default canvas, not its art. Asking for the right
# shape up front costs nothing (verified: 1.5 credits at both 1:1 and 21:9).
_AUTO = {"aspect_ratio": "auto"}
CANDIDATES: dict[str, dict | None] = {
    "kling_omni_image": {**_AUTO, "resolution": "2k"},   # 0.50 — 2k costs the same as 1k
    "gpt_image_2": {**_AUTO, "quality": "low"},          # 0.75 — 7.0 at its default quality
    "seedream_v4_5": {**_AUTO, "quality": "high"},       # 1.00 — high costs the same as basic
    "nano_banana_flash": {**_AUTO, "resolution": "1k"},  # 1.50 — the incumbent default
    "flux_kontext": dict(_AUTO),                         # 1.50 — no other tuning params
    "nano_banana_pro": {**_AUTO, "resolution": "1k"},    # 2.00 — the quality ceiling
}

# The fixed panel. Spans large/small and ui_element/icon so a model that only handles big
# rectangles is caught by ShopNavIcon rather than flattered by PlayButton.
PANEL_POLISH = ["PlayButton", "AvatarFrame", "GemCurrencyPill", "ShopNavIcon"]
PANEL_TEXT = ["PlayButton", "NavBarFrame"]
PANEL_ISOLATE = [("PlayButton", "PLAY"), ("NavBarFrame", "Shop")]

# Stage 0 spends as little as possible to answer "can this model do the job at all" —
# one polish call each. A model that ignores the reference or won't produce a magenta
# field fails here for ~1 credit instead of ~8.
SMOKE_POLISH = ["PlayButton"]


def _run_dir(args) -> Path:
    return Path(args.run) if args.run else EXPERIMENTS_DIR / f"stage{args.stage}"


def _panel(stage: int):
    if stage == 0:
        return SMOKE_POLISH, [], []
    return PANEL_POLISH, PANEL_TEXT, PANEL_ISOLATE


def _arms(args) -> list[Arm]:
    names = [m.strip() for m in args.models.split(",")] if args.models else list(CANDIDATES)
    unknown = [n for n in names if n not in CANDIDATES]
    if unknown:
        raise SystemExit(f"unknown model(s): {', '.join(unknown)}\nknown: {', '.join(CANDIDATES)}")
    variants = [v.strip() for v in args.variants.split(",")] if args.variants else ["v1"]
    return [
        Arm(model=n, params=CANDIDATES[n], prompt_variant=v)
        for n in names for v in variants
    ]


def _build(args) -> tuple[list[Arm], list, Path]:
    out = _run_dir(args)
    out.mkdir(parents=True, exist_ok=True)
    polish, text, isolate = _panel(args.stage)
    with SessionLocal() as db:
        jobs = build_jobs(db, args.mockup, out, polish, text, isolate)
        # Explicit: this session only ever read. Rolling back makes that structural rather
        # than a promise, so an accidental future mutation can't reach the catalogue.
        db.rollback()
    if not jobs:
        raise SystemExit("no jobs built — are the panel regions built and bound on this mockup?")
    save_jobs(jobs, out / "jobs.json")
    return _arms(args), jobs, out


def cmd_plan(args) -> int:
    arms, jobs, out = _build(args)
    est = dry_run(arms, jobs)
    print(f"stage {args.stage}: {len(arms)} arm(s) x {len(jobs)} job(s) = {est['calls']} calls\n")
    print("jobs:")
    for j in jobs:
        print(f"  {j.key:44} {j.kind:14} ops={'+'.join(j.ops)}")
    print()
    print(f"{'arm':44} {'calls':>6} {'cr/call':>9} {'credits':>9}")
    print("-" * 72)
    for row in est["arms"]:
        pc = "—" if row["per_call"] is None else f"{row['per_call']:.3f}"
        cr = "—" if row["credits"] is None else f"{row['credits']:.2f}"
        note = f"  ({row['unpriced']} unpriced, charged at worst rate)" if row["unpriced"] else ""
        print(f"{row['describe'][:44]:44} {row['calls']:>6} {pc:>9} {cr:>9}{note}")
    print("-" * 72)
    print(f"{'PROJECTED TOTAL':44} {est['calls']:>6} {'':>9} {est['total_credits']:>9.2f} credits")
    if est.get("unpriced"):
        print(f"\nnote: {est['unpriced']} call(s) would not quote. They are budgeted at the "
              f"arm's highest quoted rate; a call with no rate to infer is refused rather "
              f"than run outside the ceiling.")
    print(f"\nnothing was generated and nothing was spent. to run:\n"
          f"  python tools/sweep_polish_text.py run --stage {args.stage} "
          f"--mockup {args.mockup} --budget {max(1, round(est['total_credits'] * 1.15))}")
    return 0


def cmd_run(args) -> int:
    arms, jobs, out = _build(args)
    est = dry_run(arms, jobs)
    if args.budget is None:
        raise SystemExit("--budget is required for a real run (see the `plan` subcommand)")
    if est["total_credits"] > args.budget:
        raise SystemExit(
            f"projected {est['total_credits']:.2f} credits exceeds --budget {args.budget}. "
            f"Raise the budget or narrow --models."
        )
    print(f"stage {args.stage}: {est['calls']} calls, ~{est['total_credits']:.2f} credits, "
          f"budget {args.budget}\n")

    def on_event(arm, rec):
        status = "ok " if rec.get("ok") else "ERR"
        extra = ""
        g = rec.get("grade") or {}
        if rec.get("ok"):
            extra = f"grade={g.get('headline')}"
            if g.get("chroma_ok") is False:
                extra += " CHROMA-FAIL"
        else:
            extra = str(rec.get("error"))[:90]
        print(f"  [{status}] {arm.key[:28]:28} {rec['job'][:34]:34} "
              f"{rec.get('credits') or 0:>5}cr {rec['seconds']:>5}s  {extra}", flush=True)

    budget = Budget(limit=args.budget)
    report = run_sweep(arms, jobs, out, budget=budget, on_event=on_event)
    print(f"\nspent {report['credits']} credits\n")
    report_mod.print_leaderboard(report, sort=args.sort)
    print(f"\nresults: {out / 'results.json'}")
    return 0


def _load_report(args) -> dict:
    """Load one run, or merge several into a single leaderboard.

    Merging exists because a long sweep is safer run in batches — a provider stall or a
    killed shell costs one batch instead of all of them — and batches are only useful if
    they can still be compared against each other afterwards. The control arm is identical
    across batches (it calls nothing), so duplicates are collapsed.
    """
    paths = [Path(p.strip()) for p in args.run.split(",")] if args.run else [_run_dir(args)]
    merged: dict = {}
    seen: set[str] = set()
    for p in paths:
        rep = json.loads((p / "results.json").read_text(encoding="utf-8"))
        if not merged:
            merged = {**rep, "arms": []}
        for arm in rep["arms"]:
            if arm["arm"] in seen:
                continue
            seen.add(arm["arm"])
            merged["arms"].append(arm)
    merged["credits"] = round(sum(a.get("credits") or 0 for a in merged["arms"]), 3)
    return merged


def cmd_report(args) -> int:
    report_mod.print_leaderboard(_load_report(args), sort=args.sort)
    return 0


def cmd_regrade(args) -> int:
    """Re-score already-generated images in place. Spends nothing.

    Exists because the grader is itself under development while the sweep runs, and a fix to
    a metric must not require re-buying the images it grades — the generated PNGs are the
    expensive part and they do not change.
    """
    from PIL import Image

    from app.experiments.grade import grade as grade_one
    from app.experiments.runner import control_arm

    for raw in (args.run or "").split(","):
        out = Path(raw.strip()) if raw.strip() else _run_dir(args)
        path = out / "results.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        jobs = {j.key: j for j in load_jobs(out / "jobs.json")}
        rebuilt = [control_arm(list(jobs.values()))]
        for arm in report["arms"]:
            if arm["arm"] == "control":
                continue
            for rec in arm["results"]:
                job = jobs.get(rec["job"])
                if not rec.get("ok") or job is None or not rec.get("image"):
                    continue
                with Image.open(rec["image"]) as im:
                    processed = im.convert("RGBA")
                raw_img = None
                if rec.get("raw_image") and Path(rec["raw_image"]).exists():
                    with Image.open(rec["raw_image"]) as im:
                        raw_img = im.convert("RGBA")
                judged = (rec.get("grade") or {}).get("judge")
                rec["grade"] = grade_one(job, processed, raw_img)
                if judged:  # rubric scores are independent of the pixel metrics
                    rec["grade"]["judge"] = judged
            rebuilt.append(arm)
        report["arms"] = rebuilt
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"re-graded {path}")
    return 0


def cmd_judge(args) -> int:
    out = _run_dir(args)
    path = out / "results.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    jobs = {j.key: j for j in load_jobs(out / "jobs.json")}
    print(f"judging with {args.provider} ({judge_mod.SAMPLES} samples per result)…")

    def say(arm, job, scores):
        print(f"  {arm[:30]:30} {job[:34]:34} {scores or 'unavailable'}", flush=True)

    judge_mod.judge_report(report, jobs, provider=args.provider, model=args.model, on_event=say)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote judge scores into {path}")
    return 0


def cmd_sheet(args) -> int:
    out = _run_dir(args)
    report = json.loads((out / "results.json").read_text(encoding="utf-8"))
    dest = Path(args.out) if args.out else out / f"blind-{args.job}.png"
    key = report_mod.blind_sheet(report, args.job, dest, seed=args.seed)
    print(f"blind sheet: {dest}\nkey (do not open before ranking): {key}")
    return 0


def cmd_rank(args) -> int:
    out = _run_dir(args)
    path = out / "results.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    key = Path(args.key) if args.key else (out / f"blind-{args.job}.key.json")
    order = [s.strip().upper() for s in args.order.split(",")]
    report_mod.apply_ranking(report, args.job, key, order)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"recorded ranking {order} for {args.job}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_mockup=True):
        if need_mockup:
            p.add_argument("--mockup", type=int, default=8)
            p.add_argument("--stage", type=int, default=0, choices=[0, 1, 2, 3])
            p.add_argument("--models", help="comma-separated subset of the candidate pool")
            p.add_argument("--variants", help="comma-separated prompt variants (default v1)")
        p.add_argument("--run", help="run directory (default storage/experiments/stage<N>)")

    p = sub.add_parser("plan", help="print arms, jobs and projected credits; spends nothing")
    common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="execute a stage (requires --budget)")
    common(p)
    p.add_argument("--budget", type=float, help="hard credit ceiling, checked before every call")
    p.add_argument("--sort", default="grade", choices=["grade", "value"])
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="print the leaderboard for a finished run")
    common(p, need_mockup=False)
    p.add_argument("--sort", default="grade", choices=["grade", "value"])
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("regrade", help="re-score existing images after a grader change; spends nothing")
    common(p, need_mockup=False)
    p.set_defaults(func=cmd_regrade)

    p = sub.add_parser("judge", help="add vision-LLM rubric scores to a finished run")
    common(p, need_mockup=False)
    p.add_argument("--provider", default="claude")
    p.add_argument("--model", default="")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser("sheet", help="render a blind, shuffled comparison sheet for one job")
    common(p, need_mockup=False)
    p.add_argument("--job", required=True)
    p.add_argument("--out")
    p.add_argument("--seed", type=int)
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("rank", help="record your blind ranking (e.g. --order C,A,D,B)")
    common(p, need_mockup=False)
    p.add_argument("--job", required=True)
    p.add_argument("--order", required=True)
    p.add_argument("--key")
    p.set_defaults(func=cmd_rank)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
