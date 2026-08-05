"""Composite exported sprites into a full-screen preview from a *.screen.json layout.

This is a verification aid: it proves the exported transparent assets are enough to
reconstruct a screen, without opening Unity. It reads the layout, resolves each
element's sprite relative to the Unity Assets/ dir, and pastes it (alpha-composited)
into its normalized rect on a reference-sized canvas.

Usage:
    python composite_screen.py --screen <Screen.screen.json> --assets <UnityProject/Assets> \
        [--out preview.png] [--bg "#1b2a4a"]
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.processing.composite import composite_layout  # noqa: E402


def parse_reference(ref: str) -> tuple[int, int]:
    try:
        w, h = ref.lower().split("x")
        return int(w), int(h)
    except Exception:
        return 1080, 1920


def build(screen_json: Path, assets_dir: Path, out: Path, bg: str | None) -> Path:
    doc = json.loads(screen_json.read_text())
    W, H = parse_reference(doc.get("reference", "1080x1920"))

    elements = []
    for el in doc.get("elements", []):
        sprite_path = assets_dir / Path(el["sprite"]).relative_to("Assets")
        if not sprite_path.exists():
            print(f"  ! missing sprite: {sprite_path}")
            continue
        with Image.open(sprite_path) as sp:
            elements.append({"image": sp.copy(), "rect": el["rect"], "z": el.get("z", 0)})
        r = el["rect"]
        print(f"  + {el['asset']:<16} at ({round(r['x']*W)},{round(r['y']*H)}) {round(r['w']*W)}x{round(r['h']*H)}")

    canvas = composite_layout(elements, W, H, bg)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"wrote {out} ({W}x{H})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", required=True, type=Path)
    ap.add_argument("--assets", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--bg", default=None, help="hex background e.g. #1b2a4a; omit for checkerboard")
    args = ap.parse_args()
    out = args.out or args.screen.with_suffix(".preview.png")
    build(args.screen, args.assets, out, args.bg)


if __name__ == "__main__":
    main()
