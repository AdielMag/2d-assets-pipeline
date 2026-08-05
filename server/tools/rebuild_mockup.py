"""Rebuild every region of a mockup through the extraction pipeline, in dependency order.

Mirrors what `_build_atlas` does for `mode="extract"` without needing the HTTP server up,
so a pipeline change can be measured from the command line:

    .venv/Scripts/python.exe tools/rebuild_mockup.py --mockup 8

Occluders are built before the frames they sit on, because de-occlusion erases each
occluder's own extracted silhouette from the screenshot and can only do that once the
occluder has a sprite (see `_occluder_mask`).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal                                    # noqa: E402
from app.models import Atlas, Mockup, MockupRegion                 # noqa: E402
from app.routers.mockups import (                                  # noqa: E402
    _extract_asset_for_region, _resolve_containment,
)


class Log:
    """A stand-in for the SSE Emitter: prints instead of streaming."""

    is_stopped = False

    def __init__(self, verbose: bool):
        self.verbose = verbose

    def emit(self, stage, status, message="", **kw):
        if self.verbose and message:
            print(f"    [{stage}/{status}] {message}")

    def preview(self, *a, **kw):
        return None

    def stop(self):
        self.is_stopped = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mockup", type=int, required=True)
    ap.add_argument("--atlas", type=int, default=None, help="atlas id to file assets under")
    ap.add_argument("--only", help="comma-separated region names, for iterating on one case")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    mockup = db.get(Mockup, args.mockup)
    if mockup is None:
        print(f"no mockup {args.mockup}")
        return 1

    atlas_id = args.atlas
    if atlas_id is None:
        atlas = db.query(Atlas).filter(Atlas.project_id == mockup.project_id).first()
        atlas_id = atlas.id if atlas else None

    bg_ids, drop_ids = _resolve_containment(list(mockup.regions))
    wanted = {n.strip() for n in args.only.split(",")} if args.only else None
    regions = [r for r in mockup.regions if r.id not in drop_ids]
    if wanted:
        regions = [r for r in regions if r.name in wanted]
    # Occluders first, frames second — the ordering de-occlusion depends on.
    regions.sort(key=lambda r: r.id in bg_ids)

    log = Log(args.verbose)
    ok, failed = 0, []
    for i, region in enumerate(regions, 1):
        kind = "frame" if region.id in bg_ids else "element"
        started = time.time()
        try:
            fresh = db.get(MockupRegion, region.id)
            asset = _extract_asset_for_region(
                db, fresh, atlas_id=atlas_id, emit=log, background=fresh.id in bg_ids,
            )
            version = asset.selected_version
            score = (version.fidelity or {}).get("score") if version else None
            got = f"{score:.1f}" if score is not None else "  - "
            print(f"[{i:2d}/{len(regions)}] {region.name:24s} {kind:8s} "
                  f"score {got:>5s}  {time.time() - started:5.1f}s")
            ok += 1
        except Exception as e:
            print(f"[{i:2d}/{len(regions)}] {region.name:24s} FAILED: {type(e).__name__}: {e}")
            failed.append(region.name)
    print(f"\n{ok} built, {len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
