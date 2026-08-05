"""Re-run background removal on every selected version's RAW image with the current
transparency algorithm, overwriting the processed image. Use after improving
processing/transparency.py. Run standalone (fresh import, not through the server)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Asset, AssetVersion  # noqa: E402
from app.processing.transparency import remove_background  # noqa: E402
from app.storage import abs_path, new_asset_path, rel_path  # noqa: E402

PROJECT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main() -> None:
    db = SessionLocal()
    assets = db.query(Asset).filter(Asset.project_id == PROJECT_ID).all()
    for a in assets:
        v = db.get(AssetVersion, a.selected_version_id) if a.selected_version_id else None
        if not v or not v.raw_path:
            continue
        raw = abs_path(v.raw_path)
        if not raw.exists():
            print(f"  ! {a.name}: raw missing"); continue
        dest = new_asset_path(db, a, a.name)
        with Image.open(raw) as img:
            remove_background(img).save(dest)
        v.processed_path = rel_path(dest)
        db.commit()
        print(f"  reprocessed {a.name}")
    db.close()


if __name__ == "__main__":
    main()
