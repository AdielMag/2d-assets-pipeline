"""Re-extract every region of a mockup and score the result, without going through HTTP.

The A/B harness for the extraction pipeline. `build-atlas` deliberately reuses an asset
that already exists for a region, which is right for a user clicking Build twice and
exactly wrong for measuring a change to the extractor — the reused asset predates the
change. Calling `_extract_asset_for_region` directly sidesteps the reuse path; it appends
a new version and selects it, so previous versions stay on disk for comparison.

    python tools/rebuild.py --mockup 6 [--generate] [--method sam2] [--no-inpaint]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402
from app.models import Atlas, Mockup  # noqa: E402
from app.routers.mockups import (  # noqa: E402
    _extract_asset_for_region, _generate_asset_for_region, _resolve_containment,
)
from app.scoring import score_mockup  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mockup", type=int, required=True)
    ap.add_argument("--method", default="auto", help="auto | classical | sam2")
    ap.add_argument("--generate", action="store_true", help="use the provider path instead")
    ap.add_argument("--provider", default="antigravity")
    ap.add_argument("--no-inpaint", action="store_true", help="keep frames occluded")
    ap.add_argument("--only", help="comma-separated region names")
    args = ap.parse_args()

    with SessionLocal() as db:
        mockup = db.get(Mockup, args.mockup)
        if mockup is None:
            print(f"No mockup {args.mockup}")
            return 1
        atlas = db.query(Atlas).filter(Atlas.project_id == mockup.project_id).first()
        bg_ids, drop_ids = _resolve_containment(list(mockup.regions))
        only = {n.strip() for n in args.only.split(",")} if args.only else None

        regions = [
            r for r in mockup.regions
            if r.id not in drop_ids and (only is None or r.name in only)
        ]
        print(f"Rebuilding {len(regions)} region(s) of mockup {mockup.id}\n")
        for i, region in enumerate(regions, 1):
            is_bg = region.id in bg_ids and not args.no_inpaint
            tag = " [frame]" if is_bg else ""
            print(f"  {i:2}/{len(regions)}  {region.name}{tag}", end="", flush=True)
            t = time.time()
            try:
                if args.generate:
                    _generate_asset_for_region(
                        db, region, args.provider, atlas_id=atlas.id if atlas else None,
                        background=is_bg,
                    )
                else:
                    _extract_asset_for_region(
                        db, region, atlas_id=atlas.id if atlas else None,
                        background=is_bg, method=args.method,
                    )
                print(f"   {time.time() - t:5.1f}s")
            except Exception as e:
                print(f"   FAILED: {type(e).__name__}: {e}")

        print()
        report = score_mockup(db, mockup)
        for entry in report["regions"]:
            f = entry.get("fidelity")
            print(f"  {entry['name']:<28} {f['score']:6.2f}" if f else f"  {entry['name']:<28}     --")
        print(f"\nmean {report['mean_score']}   worst {report['min_score']}   "
              f"bound {report['bound']}/{report['total']}")
        screen = report.get("screen")
        if screen:
            print(f"SCREEN  score {screen['score']}   ΔE {screen['delta_e']}   "
                  f"SSIM {screen['ssim']}   filled {screen['filled'] * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
