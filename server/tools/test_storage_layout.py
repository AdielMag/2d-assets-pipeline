"""End-to-end check of the domain-mirrored layout against a running server.

Creates its OWN throwaway project and deletes it at the end — it never touches real
assets, because half of what it asserts is that a delete really removes files.

    python -m tools.test_storage_layout          # server must be on :8787
"""
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app.config import STORAGE_DIR  # noqa: E402

BASE = "http://localhost:8787"
failures: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


def call(method: str, path: str, body=None, data=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=data)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def upload_asset(project_id: int, name: str, atlas_id: int | None) -> dict:
    """POST a magenta-keyed PNG through the real import endpoint."""
    img = Image.new("RGB", (64, 64), (255, 0, 255))
    img.paste((20, 180, 90), (16, 16, 48, 48))
    buf = io.BytesIO()
    img.save(buf, "PNG")

    boundary = "----storagelayouttest"
    parts = []
    for field, value in (("name", name), ("type", "icon"),
                         ("atlas_id", "" if atlas_id is None else str(atlas_id))):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n".encode() + buf.getvalue() + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return call(
        "POST", f"/api/projects/{project_id}/assets/import-cutout",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def files_of(asset: dict) -> list[Path]:
    out = []
    for v in asset.get("versions", []):
        for key in ("raw_path", "processed_path"):
            if v.get(key):
                out.append(STORAGE_DIR / v[key])
    return out


def main() -> int:
    project = call("POST", "/api/projects", {"name": "ZZ Storage Layout Test"})
    pid = project["id"]
    print(f"scratch project {pid}")
    try:
        outer = call("POST", f"/api/projects/{pid}/atlases", {"name": "Outer"})
        inner = call("POST", f"/api/projects/{pid}/atlases", {"name": "Inner", "parent_id": outer["id"]})

        # 1. an asset lands in its own folder, under its domain chain
        asset = upload_asset(pid, "WidgetIcon", inner["id"])
        folder = STORAGE_DIR / f"projects/{pid}/domains/Outer/Inner/WidgetIcon"
        check("asset files land in domains/Outer/Inner/WidgetIcon",
              folder.is_dir() and all(f.exists() and f.parent == folder for f in files_of(asset)))
        check("both files (raw + processed) are in there", len(files_of(asset)) == 2)

        # 2. moving it to another domain moves the folder and keeps the paths valid
        moved = call("PATCH", f"/api/assets/{asset['id']}", {"atlas_id": outer["id"]})
        new_folder = STORAGE_DIR / f"projects/{pid}/domains/Outer/WidgetIcon"
        check("moving domain moves the folder", new_folder.is_dir() and not folder.exists())
        check("moved files still exist where the DB says", all(f.exists() for f in files_of(moved)))
        check("moved files serve over HTTP", all(
            urllib.request.urlopen(
                BASE + "/storage/" + "/".join(urllib.parse.quote(s) for s in v.split("/"))
            ).status == 200
            for ver in moved["versions"] for v in (ver["raw_path"], ver["processed_path"])
        ))

        # 3. renaming the domain moves the whole subtree
        call("PATCH", f"/api/atlases/{outer['id']}", {"name": "Renamed Outer"})
        renamed = call("GET", f"/api/assets/{asset['id']}")
        renamed_folder = STORAGE_DIR / f"projects/{pid}/domains/Renamed Outer/WidgetIcon"
        check("renaming a domain renames its folder", renamed_folder.is_dir())
        check("paths follow the rename", all(f.exists() for f in files_of(renamed)))

        # 4. renaming the asset renames its folder
        call("PATCH", f"/api/assets/{asset['id']}", {"name": "Renamed Widget"})
        after = call("GET", f"/api/assets/{asset['id']}")
        check("renaming an asset renames its folder",
              (STORAGE_DIR / f"projects/{pid}/domains/Renamed Outer/Renamed Widget").is_dir()
              and not renamed_folder.exists()
              and all(f.exists() for f in files_of(after)))

        # 5. deleting the asset deletes its files
        doomed_files = files_of(after)
        call("DELETE", f"/api/assets/{asset['id']}")
        check("deleting an asset deletes its folder",
              not (STORAGE_DIR / f"projects/{pid}/domains/Renamed Outer/Renamed Widget").exists()
              and not any(f.exists() for f in doomed_files))

        # 6. a cascade domain delete takes its assets' files with it
        keep = upload_asset(pid, "KeepMe", None)
        doomed = upload_asset(pid, "DoomedIcon", inner["id"])
        doomed_files = files_of(doomed)
        keep_files = files_of(keep)
        call("DELETE", f"/api/atlases/{inner['id']}?cascade=true")
        check("cascade delete removes the domain's asset files", not any(f.exists() for f in doomed_files))
        check("cascade delete removes the domain folder",
              not (STORAGE_DIR / f"projects/{pid}/domains/Renamed Outer/Inner").exists())
        check("an unrelated asset is untouched", all(f.exists() for f in keep_files))

        # 7. a plain domain delete keeps the art (assets are just unassigned)
        survivor = upload_asset(pid, "SurvivorIcon", outer["id"])
        call("DELETE", f"/api/atlases/{outer['id']}")
        survivor = call("GET", f"/api/assets/{survivor['id']}")
        check("plain domain delete keeps the art", all(f.exists() for f in files_of(survivor)))
        check("...and files it under _unassigned",
              all("/_unassigned/" in v["processed_path"] for v in survivor["versions"]))

        # 8. deleting a mockup deletes its screenshot
        img = Image.new("RGB", (32, 32), (10, 10, 10))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        boundary = "----mockuptest"
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"m.png\"\r\n"
            f"Content-Type: image/png\r\n\r\n".encode() + buf.getvalue()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        mockup = call("POST", f"/api/projects/{pid}/mockups/upload", data=payload,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        shot = STORAGE_DIR / mockup["image_path"]
        check("a mockup lands in its own folder",
              shot.exists() and shot.parent.name == str(mockup["id"]))
        call("DELETE", f"/api/mockups/{mockup['id']}")
        check("deleting a mockup deletes its screenshot", not shot.exists())
    finally:
        call("DELETE", f"/api/projects/{pid}")
        gone = not (STORAGE_DIR / "projects" / str(pid)).exists()
        check("deleting a project deletes its storage folder", gone)

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
