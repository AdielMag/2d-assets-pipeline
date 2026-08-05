"""Finalize ClashUp assets from their RAW images: background-key, then either
- 9-slice assets (buttons/panels): tight trim + detect borders, so they fill their rect;
- everything else (icons/badges/sprite): trim + centered padding, and clear nine_slice.

Idempotent — always works from the stored raw image. Run standalone after generation:
    .venv/Scripts/python.exe tools/finalize.py [project_id]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

import clashup_data as data  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Asset, AssetVersion  # noqa: E402
from app.processing.nine_slice import detect_borders  # noqa: E402
from app.processing.transparency import remove_background  # noqa: E402
from app.processing.trim import trim_and_pad, trim_to_content  # noqa: E402
from app.storage import abs_path, new_asset_path, rel_path  # noqa: E402

PROJECT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 2
# name -> key, to look up whether an asset should be 9-sliced
NAME_TO_KEY = {v[0]: k for k, v in data.ASSETS.items()}


def main() -> None:
    db = SessionLocal()
    for a in db.query(Asset).filter(Asset.project_id == PROJECT_ID).all():
        v = db.get(AssetVersion, a.selected_version_id) if a.selected_version_id else None
        if not v or not v.raw_path:
            continue
        raw = abs_path(v.raw_path)
        if not raw.exists():
            print(f"  ! {a.name}: raw missing"); continue

        key = NAME_TO_KEY.get(a.name)
        nine = key in data.NINE_SLICE
        with Image.open(raw) as img:
            keyed = remove_background(img)
        if nine:
            final = trim_to_content(keyed)          # tight: fills its rect for 9-slice
            a.nine_slice = detect_borders(final)
        else:
            final = trim_and_pad(keyed)              # centered with padding
            a.nine_slice = None

        dest = new_asset_path(db, a, a.name)
        final.save(dest)
        v.processed_path = rel_path(dest)
        db.commit()
        print(f"  {'9slice' if nine else 'padded'}  {a.name}  {final.size}")
    db.close()


if __name__ == "__main__":
    main()
