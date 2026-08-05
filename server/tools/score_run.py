"""Fidelity harness — the A/B instrument for every change to the pipeline.

Scores every bound region on a mockup against the pixels it was cut from, plus the whole
rebuilt screen against the original screenshot, and prints a table. Save a run with
--save and compare later runs against it with --compare to see whether a change actually
improved the output instead of guessing from a thumbnail.

    # capture where we are today
    python tools/score_run.py --mockup 2 --save baseline.json

    # after a change
    python tools/score_run.py --mockup 2 --compare baseline.json

`--bind-existing` binds unbound regions to already-generated assets by name (the same
reuse matching `_build_atlas` does) *without* generating anything, so a freshly detected
screen can be scored against the existing asset catalogue for free.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import Asset, Mockup  # noqa: E402
from app.routers.mockups import _name_tokens  # noqa: E402
from app.scoring import score_mockup  # noqa: E402

# Minimum token overlap (Jaccard) for a detected region name to be considered the same
# element as a catalogue asset.
MATCH_THRESHOLD = 0.6


def bind_existing(db, mockup: Mockup) -> int:
    """Bind unbound regions to existing assets by name so a freshly detected screen can
    be scored against the existing catalogue without generating anything.

    Matches on *token overlap* rather than the exact slug `_build_atlas` uses: the vision
    detector writes `AvatarFrame` where the catalogue holds `Avatar Frame`, whose slugs
    ('avatarframe' / 'avatar-frame') neither match nor contain each other. Reuses
    `_name_tokens`, which already splits PascalCase for the sibling-family logic.

    Asset type is a tie-breaker, not a gate — the pipeline itself disagrees about whether
    an AvatarFrame is a `ui_element` or an `icon`, and for a measurement run it is better
    to score a type-mismatched match than to score nothing."""
    catalogue = db.query(Asset).filter(
        Asset.project_id == mockup.project_id,
        Asset.selected_version_id.is_not(None),
    ).all()

    bound = 0
    for region in mockup.regions:
        if region.asset_id is not None:
            continue
        want = _name_tokens(region.name)
        if not want:
            continue

        best, best_key = None, (0.0, 0)
        for asset in catalogue:
            have = _name_tokens(asset.name)
            union = want | have
            if not union:
                continue
            jaccard = len(want & have) / len(union)
            if jaccard < MATCH_THRESHOLD:
                continue
            key = (jaccard, 1 if asset.type == region.asset_type else 0)
            if key > best_key:
                best, best_key = asset, key

        if best is not None:
            region.asset_id = best.id
            bound += 1
    if bound:
        db.commit()
    return bound


def _fmt(value, width: int, spec: str = "") -> str:
    if value is None:
        return "—".rjust(width)
    return f"{value:{spec}}".rjust(width)


def print_report(report: dict, previous: dict | None = None) -> None:
    prev_by_name = {}
    if previous:
        prev_by_name = {
            r["name"]: (r.get("fidelity") or {}).get("score")
            for r in previous.get("regions", [])
        }

    header = f"{'region':28} {'type':12} {'ΔE':>7} {'SSIM':>7} {'cover':>7} {'score':>7}"
    if previous:
        header += f" {'Δ':>8}"
    print(header)
    print("-" * len(header))

    for row in report["regions"]:
        f = row.get("fidelity")
        line = f"{row['name'][:28]:28} {row['type'][:12]:12}"
        if not f:
            line += f" {'unbound':>39}" if row["asset_id"] is None else f" {'no image':>39}"
        else:
            line += (
                f" {_fmt(f['delta_e'], 7, '.2f')}"
                f" {_fmt(f['ssim'], 7, '.3f')}"
                f" {_fmt(f['coverage'], 7, '.3f')}"
                f" {_fmt(f['score'], 7, '.2f')}"
            )
            if previous:
                old = prev_by_name.get(row["name"])
                if old is None:
                    line += f" {'new':>8}"
                else:
                    delta = f["score"] - old
                    line += f" {delta:>+8.2f}"
        print(line)

    print("-" * len(header))
    print(f"bound {report['bound']}/{report['total']}   mean {_fmt(report['mean_score'], 6, '.2f')}"
          f"   worst {_fmt(report['min_score'], 6, '.2f')}")

    screen = report.get("screen")
    if screen:
        print(
            f"\nWHOLE SCREEN   ΔE {screen['delta_e']:.2f}   SSIM {screen['ssim']:.3f}"
            f"   filled {screen['filled'] * 100:.1f}%   score {screen['score']:.2f}"
        )
        if previous and previous.get("screen"):
            print(f"               vs previous: {screen['score'] - previous['screen']['score']:+.2f}")
        if screen.get("missing"):
            print(f"               missing: {', '.join(screen['missing'])}")

    if previous and report.get("mean_score") is not None and previous.get("mean_score") is not None:
        print(f"\nMEAN DELTA: {report['mean_score'] - previous['mean_score']:+.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mockup", type=int, required=True, help="mockup id to score")
    ap.add_argument("--save", type=Path, help="write the report to this JSON file")
    ap.add_argument("--compare", type=Path, help="diff against a previously saved report")
    ap.add_argument("--bind-existing", action="store_true",
                    help="bind unbound regions to existing assets by name first (no generation)")
    ap.add_argument("--no-screen", action="store_true", help="skip the whole-screen score")
    args = ap.parse_args()

    with SessionLocal() as db:
        mockup = db.get(Mockup, args.mockup)
        if mockup is None:
            print(f"No mockup with id {args.mockup}", file=sys.stderr)
            return 1
        if args.bind_existing:
            n = bind_existing(db, mockup)
            print(f"Bound {n} region(s) to existing assets by name.\n")
        report = score_mockup(db, mockup, include_screen=not args.no_screen)

    previous = None
    if args.compare:
        if args.compare.exists():
            previous = json.loads(args.compare.read_text(encoding="utf-8"))
        else:
            print(f"(no baseline at {args.compare}, showing absolute scores)\n", file=sys.stderr)

    print_report(report, previous)

    if args.save:
        args.save.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved report to {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
