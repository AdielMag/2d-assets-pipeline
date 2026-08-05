"""Delete all Screen 5 (Mockup 6) assets, re-detect regions, and generate all assets
using Antigravity with gemini-3-pro-image-preview visual model."""

from app.db import SessionLocal
from app.models import Mockup, MockupRegion, Asset, AssetVersion
from app.routers.mockups import (
    _detect_regions, DetectRegionsBody, _build_atlas, BuildAtlasBody,
)
from app.progress import NoEmit
from app.providers import prefs

MOCKUP_ID = 6
ATLAS_ID = 1  # Common atlas


def main():
    # Step 0: Set visual model to gemini-3-pro-image-preview
    print("Setting visual model to gemini-3-pro-image-preview...")
    prefs.save({
        "provider_visual_models": {"antigravity": "gemini-3-pro-image-preview"},
    })
    print(f"  Visual model now: {prefs.get_provider_visual_model('antigravity')}")

    with SessionLocal() as db:
        mockup = db.get(Mockup, MOCKUP_ID)
        if not mockup:
            print(f"Mockup {MOCKUP_ID} not found")
            return

        # Step 1: Unbind and delete all assets tied to this mockup's regions
        print(f"\n=== Step 1: Cleaning up Mockup {MOCKUP_ID} ===")
        asset_ids = set()
        for r in mockup.regions:
            if r.asset_id:
                asset_ids.add(r.asset_id)
            if r.icon_asset_id:
                asset_ids.add(r.icon_asset_id)
            r.asset_id = None
            r.icon_asset_id = None
        db.commit()

        for aid in asset_ids:
            asset = db.get(Asset, aid)
            if asset:
                for v in db.query(AssetVersion).filter_by(asset_id=aid).all():
                    db.delete(v)
                db.delete(asset)

        for r in list(mockup.regions):
            db.delete(r)
        db.commit()
        db.refresh(mockup)
        print(f"  Deleted {len(asset_ids)} assets, cleared all regions")

        # Step 2: Auto-detect regions
        print(f"\n=== Step 2: Auto-detecting regions ===")
        body = DetectRegionsBody()
        _detect_regions(db, mockup, body, NoEmit())
        db.refresh(mockup)
        print(f"  Detected {len(mockup.regions)} regions:")
        for r in mockup.regions:
            print(f"    [{r.name}] ({r.asset_type}, sliced={r.asset_type == 'ui_element'})")
            print(f"      prompt: {r.prompt[:120]}...")

        # Step 3: Generate all assets
        print(f"\n=== Step 3: Generating all assets (antigravity + gemini-3-pro-image-preview) ===")
        build_body = BuildAtlasBody(atlas_id=ATLAS_ID, provider="antigravity")
        res = _build_atlas(db, mockup, build_body, NoEmit())

        print(f"\n=== Done! ===")
        print(f"  Generated: {len(res.get('results', []))}")
        for item in res.get("results", []):
            print(f"    Region {item.get('region_id')}: asset_id={item.get('asset_id')}, reused={item.get('reused')}")
        if res.get("errors"):
            print(f"  Errors: {len(res['errors'])}")
            for err in res["errors"]:
                print(f"    {err.get('name', err.get('region_id'))}: {err.get('error')}")


if __name__ == "__main__":
    main()
