"""Render a mockup's bound sprites onto magenta at 1:1 — the only way alpha defects show.

A sheet composited on white hides exactly what matters here: a missing chunk, a soft skirt
of the old backdrop, a ragged rim. Magenta appears nowhere in the artwork, so anything
pink is a hole and anything with a pink fringe is a bad edge.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Asset, Mockup  # noqa: E402
from app.storage import abs_path  # noqa: E402

MAGENTA = (255, 0, 255, 255)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mockup", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--cell", type=int, default=340)
    args = ap.parse_args()

    db = SessionLocal()
    mockup = db.get(Mockup, args.mockup)
    if mockup is None:
        print(f"no mockup {args.mockup}")
        return 1

    tiles = []
    for region in sorted(mockup.regions, key=lambda r: r.name):
        if region.asset_id is None:
            continue
        asset = db.get(Asset, region.asset_id)
        version = asset.selected_version if asset else None
        if version is None:
            continue
        path = abs_path(version.processed_path or version.raw_path)
        if not path.exists():
            continue
        with Image.open(path) as im:
            sprite = im.convert("RGBA").copy()
        sprite.thumbnail((args.cell, args.cell - 18), Image.LANCZOS)
        cell = Image.new("RGBA", (args.cell, args.cell), (24, 24, 28, 255))
        plate = Image.new("RGBA", sprite.size, MAGENTA)
        plate.alpha_composite(sprite)
        cell.paste(plate, ((args.cell - sprite.width) // 2, 18))
        score = (version.fidelity or {}).get("score")
        label = f"{region.name}  {score:.0f}" if score is not None else region.name
        ImageDraw.Draw(cell).text((6, 4), label, fill=(235, 235, 240, 255))
        tiles.append(cell)

    if not tiles:
        print("nothing bound to render")
        return 1
    cols = args.cols
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * args.cell, rows * args.cell), (24, 24, 28))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % cols) * args.cell, (i // cols) * args.cell))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    print(f"{len(tiles)} sprites -> {args.out} ({sheet.width}x{sheet.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
