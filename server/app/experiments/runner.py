"""Executes a sweep: every arm runs every job, results land on disk, nothing touches the DB.

The generation path here deliberately mirrors `routers.mockups._llm_reference_pass` and the
post-processing that follows it in `_redraw_built_regions` — same prompt composition, same
provider call, same background removal, same trim — because a measurement of a different
pipeline than the one that ships is worthless. What it does not mirror is the persistence:
no AssetVersion, no `selected_version_id`, no resolution bump, no region deletion.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..processing.transparency import remove_background
from ..processing.trim import trim_for_fit
from ..progress import is_quota_error
from ..prompting import CHROMA_HINT, reference_instruction
from ..providers import get_enabled_provider
from ..providers.base import ProviderError
from .grade import grade
from .plan import Arm, Job

# Matches the build pipeline's own ceiling. Higgsfield rate-limits per account, and three
# in flight is the figure `_build_atlas` already runs at without tripping it.
MAX_WORKERS = 3


@dataclass
class Budget:
    """A hard credit ceiling, checked before every call rather than only at the start.

    Checked per call because a sweep's real cost is only known once each model has quoted
    its own price for the actual prompt — an upfront estimate that drifts is exactly how a
    'roughly 50 credits' run becomes 300.
    """
    limit: float
    spent: float = 0.0
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self):
        self._lock = threading.Lock()

    def try_spend(self, credits: float) -> bool:
        with self._lock:
            if self.spent + credits > self.limit:
                return False
            self.spent += credits
            return True

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.spent)


def compose(job: Job, arm: Arm) -> str:
    """The exact prompt `_llm_reference_pass` would send for this job and wording."""
    return reference_instruction(job.ops, arm.prompt_variant) + CHROMA_HINT


_quote_memo: dict[tuple, float | None] = {}
_quote_lock = threading.Lock()


def quote(provider, job: Job, arm: Arm, retries: int = 1) -> float | None:
    """Real credit price for this exact call, from the provider's own pricing. None when
    the provider has no such notion (Antigravity) or the CLI could not be reached.

    Retried once because `estimate_cost` is best-effort by design — it swallows timeouts and
    returns None — and an unpriced call is not a free call. Observed live: one of eight
    quotes failed transiently, which would otherwise have let that call spend outside the
    budget ceiling entirely.
    """
    est = getattr(provider, "estimate_cost", None)
    if est is None:
        return None
    # A quote is a pure function of (model, params, prompt, reference), and each one costs a
    # CLI round trip. Without this the price of every call is fetched twice — once by the
    # `plan`/budget check and again by the run — which for a 48-call stage is ~96 subprocess
    # spawns and several minutes before a single image is generated.
    memo_key = (arm.model, tuple(sorted((arm.params or {}).items())), arm.prompt_variant, job.key)
    with _quote_lock:
        if memo_key in _quote_memo:
            return _quote_memo[memo_key]
    for attempt in range(retries + 1):
        price = est(
            reference_instruction(job.ops, arm.prompt_variant),
            model=arm.model, reference_images=[Path(job.ref_path)], params=arm.params,
        )
        if price is not None:
            with _quote_lock:
                _quote_memo[memo_key] = price
            return price
        if attempt < retries:
            time.sleep(1.0)
    # A failure is deliberately NOT memoised — it is transient (a CLI timeout), and caching
    # it would turn one hiccup into a permanently unpriced call for the rest of the run.
    return None


def quote_all(provider, jobs: list[Job], arm: Arm) -> tuple[dict[str, float | None], float | None]:
    """Price every job for an arm up front, plus a fallback for any that would not quote.

    The fallback is the *highest* price the arm did quote: a call whose cost is unknown must
    be charged against the budget at the worst plausible rate, or the ceiling is not a
    ceiling. Quoting up front also keeps CLI latency out of the timed run.
    """
    prices = {j.key: quote(provider, j, arm) for j in jobs}
    known = [p for p in prices.values() if p is not None]
    return prices, (max(known) if known else None)


def run_job(provider, job: Job, arm: Arm, out_dir: Path) -> dict:
    """One provider call plus its post-processing and grading. Never raises — a failed arm
    is a result ("this model cannot do this"), not an abort, and the caller needs the other
    jobs' numbers regardless."""
    started = time.time()
    raw_path = out_dir / f"{job.key}-raw.png"
    out_path = out_dir / f"{job.key}.png"
    record: dict = {"job": job.key, "kind": job.kind, "region": job.region_name}
    try:
        provider.generate(
            compose(job, arm), raw_path, reference_images=[Path(job.ref_path)],
            transparent=True, model=arm.model, reference_mode=True, params=arm.params,
        )
        record["command"] = provider.last_command
        with Image.open(raw_path) as im:
            raw = im.convert("RGBA")
            processed = remove_background(im) if provider.needs_transparency_postprocess else raw
        # Same shaping the real steps apply before storing: the model draws on its own
        # canvas, so the result is cropped to content and padded back to the reference's
        # true pixel ratio. Skipping it would grade a differently-shaped image.
        processed = trim_for_fit(processed, job.sliced, aspect_ratio=job.ref_ratio)
        processed.save(out_path)
        record["image"] = str(out_path)
        record["raw_image"] = str(raw_path)
        record["grade"] = grade(job, processed, raw)
        record["ok"] = True
    except ProviderError as e:
        record.update(ok=False, error=str(e), quota=is_quota_error(str(e)))
    except Exception as e:  # a decode/processing failure is still a result for this arm
        record.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-800:])
    record["seconds"] = round(time.time() - started, 1)
    return record


def run_arm(
    arm: Arm, jobs: list[Job], out_root: Path, provider_name: str = "higgsfield",
    budget: Budget | None = None, on_event=None,
) -> dict:
    """Run every job for one arm. Halts the arm on a quota/rate-limit error rather than
    burning the rest of the budget discovering the same thing seven more times — the same
    rule `_build_atlas` applies to a build run."""
    provider = get_enabled_provider(provider_name)
    out_dir = out_root / arm.key
    out_dir.mkdir(parents=True, exist_ok=True)
    say = on_event or (lambda *a, **k: None)

    results: list[dict] = []
    halted: str | None = None
    lock = threading.Lock()
    prices, fallback = quote_all(provider, jobs, arm)

    def one(job: Job) -> dict | None:
        nonlocal halted
        with lock:
            if halted:
                return None
        price = prices.get(job.key)
        assumed = price is None
        if assumed:
            price = fallback
        if budget is not None:
            if price is None:
                # No quote and nothing to infer one from. Spending here would be spending an
                # unknown amount against a hard ceiling, so the call is refused instead.
                return {"job": job.key, "kind": job.kind, "region": job.region_name,
                        "ok": False, "error": "unpriced: provider would not quote this call",
                        "credits": None, "seconds": 0.0}
            if not budget.try_spend(price):
                with lock:
                    halted = halted or f"budget exhausted (needed {price}, {budget.remaining:.2f} left)"
                return None
        rec = run_job(provider, job, arm, out_dir)
        rec["credits"] = price
        rec["credits_assumed"] = assumed
        if rec.get("quota"):
            with lock:
                halted = halted or f"provider quota/rate limit: {rec.get('error')}"
        say(arm, rec)
        return rec

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(jobs)))) as pool:
        for rec in pool.map(one, jobs):
            if rec is not None:
                results.append(rec)

    return {
        "arm": arm.key,
        "model": arm.model,
        "params": arm.params,
        "prompt_variant": arm.prompt_variant,
        "describe": arm.describe(),
        "results": results,
        "halted": halted,
        "credits": round(sum(r.get("credits") or 0 for r in results), 3),
    }


def dry_run(arms: list[Arm], jobs: list[Job], provider_name: str = "higgsfield") -> dict:
    """What the sweep would cost, quoted per arm from the provider's real pricing, without
    creating a single job. This is the gate: no sweep starts until this number is seen."""
    provider = get_enabled_provider(provider_name)
    rows = []
    for arm in arms:
        prices, fallback = quote_all(provider, jobs, arm)
        per_call = list(prices.values())
        known = [c for c in per_call if c is not None]
        unpriced = len(per_call) - len(known)
        # Unpriced calls are charged at the fallback rate in the projection, exactly as the
        # runner will charge them — a total that quietly omitted them would understate the
        # cost and let a budget be set too low to finish.
        projected = sum(known) + unpriced * (fallback or 0.0)
        rows.append({
            "arm": arm.key,
            "describe": arm.describe(),
            "calls": len(jobs),
            "per_call": known[0] if known and len(set(known)) == 1 else None,
            "credits": round(projected, 3) if known else None,
            "unpriced": unpriced,
        })
    total = sum(r["credits"] or 0 for r in rows)
    return {
        "arms": rows,
        "total_credits": round(total, 3),
        "calls": len(arms) * len(jobs),
        "unpriced": sum(r["unpriced"] for r in rows),
    }


def control_arm(jobs: list[Job]) -> dict:
    """The do-nothing arm: grade the input each model was given, spending nothing.

    Included because without it a leaderboard answers the wrong question. The first stage-0
    run ranked six models against each other while all six were in fact *worse than not
    running the step at all* — the best scored 69.9 against the untouched sprite's 91.4 —
    and nothing in the table said so. A ranking with no baseline can only tell you which
    option is least bad.

    **Polish only.** Polish is optional: leaving the extracted sprite alone is a real choice,
    so "no call" is a genuine competitor. Text removal and text isolation are not — there is
    no way to obtain a de-lettered frame or an isolated caption sprite without calling a
    model, so scoring the untouched input for those compares against something that cannot
    be chosen. It also flatters it: the untouched frame is pixel-identical to the truth crop
    it is scored against, so it wins on identity precisely *by* failing the task.
    """
    results = []
    for job in jobs:
        if job.kind != "polish":
            continue
        with Image.open(job.ref_path) as im:
            ref = im.convert("RGBA")
            results.append({
                "job": job.key, "kind": job.kind, "region": job.region_name, "ok": True,
                "image": job.ref_path, "grade": grade(job, ref, None),
                "credits": 0.0, "seconds": 0.0,
            })
    return {
        "arm": "control", "model": "(none — input unchanged)", "params": None,
        "prompt_variant": "—", "describe": "CONTROL: no model call",
        "results": results, "halted": None, "credits": 0.0,
    }


def run_sweep(
    arms: list[Arm], jobs: list[Job], out_root: Path, provider_name: str = "higgsfield",
    budget: Budget | None = None, on_event=None,
) -> dict:
    out_root.mkdir(parents=True, exist_ok=True)
    arm_results = [control_arm(jobs)]
    for arm in arms:
        res = run_arm(arm, jobs, out_root, provider_name, budget, on_event)
        arm_results.append(res)
        # One arm hitting the account-wide quota means every later arm would too; stopping
        # here keeps the partial results interpretable instead of littering them with
        # identical failures.
        if res["halted"] and "quota" in res["halted"]:
            break
    report = {
        "provider": provider_name,
        "jobs": [j.key for j in jobs],
        "arms": arm_results,
        "credits": round(sum(a["credits"] for a in arm_results), 3),
    }
    (out_root / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
