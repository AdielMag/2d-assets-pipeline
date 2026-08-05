"""Driver for the ClashUp POC. Talks to the running pipeline server over HTTP.

Subcommands:
  setup     create/patch the ClashUp project + all assets (idempotent by name)
  test      one generate call to confirm image quota is available
  generate  generate every asset that has no version yet (sequential, paced)
  export    install importer + editor scripts, export assets, export both screens
  status    print project/asset/version state

Run from the server dir with the venv python, e.g.
  .venv/Scripts/python.exe tools/clashup_run.py setup
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clashup_data as data  # noqa: E402

BASE = "http://127.0.0.1:8787"
REPO = Path(__file__).resolve().parents[2]
UNITY_PATH = str(REPO / "unity-clashup")


def api(method: str, path: str, body: dict | None = None, timeout: int = 600) -> dict:
    url = BASE + path
    data_bytes = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data_bytes, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail[:400]}")


def find_project() -> dict | None:
    for p in api("GET", "/api/projects"):
        if p["name"] == data.PROJECT["name"]:
            return p
    return None


def setup() -> dict:
    proj = find_project()
    if proj is None:
        proj = api("POST", "/api/projects", {
            "name": data.PROJECT["name"],
            "style_description": data.PROJECT["style_description"],
            "palette": data.PROJECT["palette"],
        })
        print(f"created project #{proj['id']} {proj['name']}")
    else:
        print(f"reusing project #{proj['id']} {proj['name']}")

    Path(UNITY_PATH).mkdir(parents=True, exist_ok=True)
    api("PATCH", f"/api/projects/{proj['id']}", {
        "unity_path": UNITY_PATH,
        "ppu": data.PROJECT["ppu"],
        "filter_mode": data.PROJECT["filter_mode"],
        "wrap_mode": data.PROJECT["wrap_mode"],
        "power_of_two": data.PROJECT["power_of_two"],
        "style_description": data.PROJECT["style_description"],
        "palette": data.PROJECT["palette"],
    })
    print(f"unity_path = {UNITY_PATH}")

    existing = {a["name"]: a for a in api("GET", f"/api/projects/{proj['id']}/assets")}
    for key, (name, atype, prompt) in data.ASSETS.items():
        if name in existing:
            print(f"  = {name} (exists #{existing[name]['id']})")
            continue
        a = api("POST", f"/api/projects/{proj['id']}/assets",
                {"name": name, "type": atype, "prompt": prompt})
        print(f"  + {name} #{a['id']} ({atype})")
    return proj


def list_atlases(project_id: int) -> dict[str, dict]:
    return {a["name"]: a for a in api("GET", f"/api/projects/{project_id}/atlases")}


def domains() -> None:
    """Create the Common/Lobby/Searching domain tree and file every asset into its
    assigned domain, so exports pack into per-domain Unity sprite atlases."""
    proj = find_project()
    if not proj:
        print("run setup first"); return
    pid = proj["id"]

    existing = list_atlases(pid)
    for name, parent in data.DOMAINS.items():
        if name in existing:
            print(f"  = domain {name} (exists #{existing[name]['id']})")
            continue
        parent_id = existing[parent]["id"] if parent else None
        a = api("POST", f"/api/projects/{pid}/atlases", {"name": name, "parent_id": parent_id})
        existing[name] = a
        print(f"  + domain {name} #{a['id']}" + (f" (parent={parent})" if parent else ""))

    amap = asset_map(pid)
    for key, domain in data.ASSET_DOMAIN.items():
        a = amap.get(key)
        if not a:
            continue
        atlas_id = existing[domain]["id"]
        if a.get("atlas_id") == atlas_id:
            continue
        api("PATCH", f"/api/assets/{a['id']}", {"atlas_id": atlas_id})
        print(f"  {key:<16} -> {domain}")


def _ratio_string(w: float, h: float) -> str:
    if w <= 0 or h <= 0:
        return "1:1"
    r = w / h
    if abs(r - 1) < 0.02:
        return "1:1"
    return f"{r:.2f}:1"


def aspects() -> None:
    """Patch every asset's aspect_ratio from its known screen rect, so the composed
    prompt — and the copy-pasted external prompt — always requests the right shape
    instead of letting the model default to its own (often square-ish widescreen)
    canvas, which is what silently broke Icon Events when generated externally."""
    proj = find_project()
    if not proj:
        print("run setup first"); return
    amap = asset_map(proj["id"])
    for key, a in amap.items():
        loc = _asset_rect(key)
        if not loc:
            continue
        _, (x, y, w, h) = loc
        ratio = _ratio_string(w, h)
        if a.get("aspect_ratio") == ratio:
            continue
        api("PATCH", f"/api/assets/{a['id']}", {"aspect_ratio": ratio})
        print(f"  {key:<16} -> {ratio}")


def asset_map(project_id: int) -> dict[str, dict]:
    """key -> asset dict, matched by display name."""
    by_name = {a["name"]: a for a in api("GET", f"/api/projects/{project_id}/assets")}
    out = {}
    for key, (name, _, _) in data.ASSETS.items():
        if name in by_name:
            out[key] = by_name[name]
    return out


def generate_one(asset_id: int, provider: str = "gemini") -> dict:
    return api("POST", f"/api/assets/{asset_id}/generate", {"provider": provider}, timeout=600)


def test_generate(provider: str) -> None:
    proj = find_project()
    if not proj:
        print("run setup first"); return
    amap = asset_map(proj["id"])
    key = "IconCoin" if "IconCoin" in amap else next(iter(amap))
    aid = amap[key]["id"]
    print(f"test generating {key} #{aid} via {provider} ...")
    try:
        a = generate_one(aid, provider)
        print(f"OK selected_version={a.get('selected_version_id')}")
    except RuntimeError as e:
        print(f"FAILED: {e}")
        sys.exit(1)


def generate_all(provider: str, pace: float, only: list[str] | None) -> None:
    proj = find_project()
    if not proj:
        print("run setup first"); return
    amap = asset_map(proj["id"])
    keys = only or list(data.ASSETS.keys())
    todo = []
    for k in keys:
        a = amap.get(k)
        if not a:
            print(f"  ? {k} not created"); continue
        if a.get("selected_version_id") and not only:
            print(f"  = {k} already has a version, skip"); continue
        todo.append(k)
    print(f"generating {len(todo)} assets via {provider}")
    for i, k in enumerate(todo, 1):
        aid = amap[k]["id"]
        print(f"[{i}/{len(todo)}] {k} #{aid} ...", flush=True)
        try:
            generate_one(aid, provider)
            print(f"    done")
        except RuntimeError as e:
            print(f"    FAILED: {e}")
            print("stopping — resolve the error (likely quota) and re-run 'generate'.")
            sys.exit(1)
        if i < len(todo):
            time.sleep(pace)
    print("all requested assets generated.")


def trim_all() -> None:
    """Trim every generated asset to its content bbox (re-detects 9-slice) so layout
    rects map to the real art, not the 1024² transparent canvas."""
    proj = find_project()
    if not proj:
        print("run setup first"); return
    amap = asset_map(proj["id"])
    for k, a in amap.items():
        if not a.get("selected_version_id"):
            continue
        api("POST", f"/api/assets/{a['id']}/trim")
        print(f"  trimmed {k}")


STORAGE = REPO / "storage"


def _asset_rect(key: str) -> tuple[str, tuple[int, int, int, int]] | None:
    """(screen_name, rect_px) to crop this asset's reference from. Prefers the screen
    named in REFERENCE_SCREEN, else the first screen that contains the asset."""
    prefer = data.REFERENCE_SCREEN.get(key)
    order = ([prefer] if prefer else []) + list(data.SCREENS.keys())
    for screen in order:
        for k, x, y, w, h in data.SCREENS[screen]["elements"]:
            if k == key:
                return screen, (x, y, w, h)
    return None


def refgen(pace: float, only: list[str] | None) -> None:
    """Regenerate assets using a crop of the real screenshot as a reference image, so
    the result matches the source screen (shape/proportions/colors), not just the style."""
    from PIL import Image

    proj = find_project()
    if not proj:
        print("run setup first"); return
    amap = asset_map(proj["id"])
    crops_dir = STORAGE / "projects" / str(proj["id"]) / "refcrops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    keys = only or list(data.ASSETS.keys())
    imgs: dict[str, Image.Image] = {}
    for i, key in enumerate(keys, 1):
        a = amap.get(key)
        if not a:
            print(f"  ? {key} not created"); continue
        loc = _asset_rect(key)
        if not loc:
            print(f"  ? {key} has no rect"); continue
        screen, (x, y, w, h) = loc
        if screen not in imgs:
            imgs[screen] = Image.open(data.SCREEN_FILES[screen]).convert("RGB")
        src = imgs[screen]
        sw, sh = src.size
        # rects are authored on REF_W x REF_H; scale to the screenshot, pad ~6%.
        sx, sy = sw / data.REF_W, sh / data.REF_H
        px, py = int(w * sx * 0.06), int(h * sy * 0.06)
        box = (max(0, int(x * sx) - px), max(0, int(y * sy) - py),
               min(sw, int((x + w) * sx) + px), min(sh, int((y + h) * sy) + py))
        crop_path = crops_dir / f"{key}.png"
        src.crop(box).save(crop_path)
        rel = crop_path.relative_to(STORAGE).as_posix()

        base = data.ASSETS[key][2]
        prompt = (
            base + " Recreate this element exactly as shown in the reference image: "
            "same shape, proportions, colors and style. Isolated and centered, the entire "
            "element fully visible within the frame with generous empty margin around it — "
            "nothing cropped or touching the edges. Blank with no text, numbers, letters "
            "or overlaid icons."
        )
        print(f"[{i}/{len(keys)}] {key} #{a['id']} ref={rel} ...", flush=True)
        try:
            api("POST", f"/api/assets/{a['id']}/generate",
                {"provider": "gemini", "prompt": prompt, "reference_paths": [rel]}, timeout=600)
            print("    done")
        except RuntimeError as e:
            print(f"    FAILED: {e}")
            sys.exit(1)
        if i < len(keys):
            time.sleep(pace)
    print("reference-guided generation complete.")


def export(provider_unused=None) -> None:
    proj = find_project()
    if not proj:
        print("run setup first"); return
    pid = proj["id"]
    amap = asset_map(pid)

    api("POST", f"/api/projects/{pid}/export/install-importer")
    print("installed editor scripts")

    ids = [a["id"] for a in amap.values() if a.get("selected_version_id")]
    res = api("POST", f"/api/projects/{pid}/export", {"asset_ids": ids})
    print(f"exported {len(res['exported'])} assets, {len(res['errors'])} errors")
    for err in res["errors"]:
        print(f"  ! {err}")

    for screen, spec in data.SCREENS.items():
        placements = []
        for key, x, y, w, h in spec["elements"]:
            a = amap.get(key)
            if not a or not a.get("selected_version_id"):
                continue  # skip un-generated assets in the preview manifest
            placements.append({
                "asset_id": a["id"],
                "x": x / data.REF_W, "y": y / data.REF_H,
                "w": w / data.REF_W, "h": h / data.REF_H,
            })
        out = api("POST", f"/api/projects/{pid}/export/screen",
                  {"name": screen, "reference": f"{data.REF_W}x{data.REF_H}",
                   "placements": placements})
        print(f"screen {screen}: {out['path']} ({out['count']} elements)")


def status() -> None:
    proj = find_project()
    if not proj:
        print("no ClashUp project"); return
    print(f"project #{proj['id']} unity_path={proj['unity_path']}")
    amap = asset_map(proj["id"])
    have = sum(1 for a in amap.values() if a.get("selected_version_id"))
    print(f"assets: {len(amap)}/{len(data.ASSETS)} created, {have} generated")
    for k, a in amap.items():
        mark = "*" if a.get("selected_version_id") else " "
        print(f"  [{mark}] {k:<16} #{a['id']} {a['type']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["setup", "test", "generate", "refgen", "trim", "domains", "aspects", "export", "status"])
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--pace", type=float, default=6.0, help="seconds between generate calls")
    ap.add_argument("--only", nargs="*", help="generate only these asset keys")
    args = ap.parse_args()

    if args.cmd == "setup":
        setup()
    elif args.cmd == "test":
        test_generate(args.provider)
    elif args.cmd == "generate":
        generate_all(args.provider, args.pace, args.only)
    elif args.cmd == "refgen":
        refgen(args.pace, args.only)
    elif args.cmd == "trim":
        trim_all()
    elif args.cmd == "domains":
        domains()
    elif args.cmd == "aspects":
        aspects()
    elif args.cmd == "export":
        export()
    elif args.cmd == "status":
        status()


if __name__ == "__main__":
    main()
