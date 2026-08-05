"""Where every stored file belongs on disk, and how to move things back into place.

The old layout was four flat buckets per project (`assets/`, `crops/`, `refs/`,
`previews/`) holding every file every asset ever produced, keyed only by a slug and a
millisecond stamp. Nothing tied a file to the asset that owned it, so nothing could be
deleted with the asset either — a project accumulated thousands of images and a delete in
the UI left every one of them behind.

The layout here mirrors the domain tree instead::

    projects/<pid>/domains/<Domain>/<SubDomain>/<AssetName>/…the asset's files…
    projects/<pid>/domains/_unassigned/<AssetName>/…
    projects/<pid>/mockups/<mockup_id>/…the screenshot + its region crops…
    projects/<pid>/refs/…                 project-level style references
    projects/<pid>/previews/…             throwaway composite previews
    projects/<pid>/_work/…                throwaway generation intermediates
    projects/<pid>/runs/…                 run logs (owned by progress.py, untouched)

Every file an asset owns lives in that asset's own folder, so deleting the asset is
`rmtree` on one directory and moving it between domains is a directory rename.

Folder names come from user-editable names (a domain can be renamed, an asset can be
moved), so `desired_layout` recomputes the whole project from the DB and `reconcile`
moves whatever is misplaced and rewrites the DB paths that pointed at it. That is what
keeps renames, reparents and domain deletes correct without bespoke move logic in every
endpoint.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import STORAGE_DIR
from .models import Asset, AssetVersion, Atlas, Mockup, Project

DOMAINS_DIR = "domains"
UNASSIGNED_DIR = "_unassigned"
MOCKUPS_DIR = "mockups"
REFS_DIR = "refs"
PREVIEWS_DIR = "previews"
WORK_DIR = "_work"
RUNS_DIR = "runs"

# Swept by `prune_orphans`: nothing in them is ever recorded in the DB, they exist only
# so an in-flight generation has somewhere to put intermediates.
EPHEMERAL_DIRS = (PREVIEWS_DIR, WORK_DIR)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows refuses to create a file or directory with any of these stems, with or without
# an extension. A domain called "CON" is unlikely but a silent OSError on save is worse.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_name(name: str) -> str:
    """A user-supplied name as a single filesystem path segment.

    Only strips what Windows actually rejects — spaces and casing survive, so the folder
    on disk reads like the domain does in the UI.
    """
    cleaned = _INVALID_CHARS.sub("-", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Windows silently drops trailing dots/spaces, which would make the path we think we
    # wrote differ from the one that exists.
    cleaned = cleaned.rstrip(". ")
    if not cleaned or cleaned.lower() in _RESERVED:
        cleaned = f"{cleaned or 'unnamed'}_"
    return cleaned[:80]


def _dedupe(names: dict[int, str]) -> dict[int, str]:
    """Make sibling folder names unique, appending `-<id>` only to the ones that clash.

    Nothing stops two sibling domains (or two assets in one domain) from sharing a name —
    project 1 currently has three root domains all called "ChildDomain". Suffixing only
    the collisions keeps the common case clean while guaranteeing distinct paths.
    """
    counts: dict[str, int] = {}
    for value in names.values():
        counts[value.lower()] = counts.get(value.lower(), 0) + 1
    return {
        key: (value if counts[value.lower()] == 1 else f"{value}-{key}")
        for key, value in names.items()
    }


def domain_dirs(db: Session, project_id: int) -> dict[int | None, str]:
    """Storage-relative directory for every domain in the project, keyed by atlas id.

    `None` maps to the home for assets that sit in no domain at all.
    """
    atlases = db.scalars(select(Atlas).where(Atlas.project_id == project_id)).all()
    by_id = {a.id: a for a in atlases}
    children: dict[int | None, list[Atlas]] = {}
    for atlas in atlases:
        # A parent in another project (or a dangling id) would otherwise strand the whole
        # subtree; treat it as a root instead.
        parent_id = atlas.parent_id if atlas.parent_id in by_id else None
        children.setdefault(parent_id, []).append(atlas)

    root = f"projects/{project_id}/{DOMAINS_DIR}"
    out: dict[int | None, str] = {None: f"{root}/{UNASSIGNED_DIR}"}

    def walk(parent_id: int | None, prefix: str, seen: frozenset[int]) -> None:
        siblings = children.get(parent_id, [])
        names = _dedupe({a.id: safe_name(a.name) for a in siblings})
        for atlas in siblings:
            if atlas.id in seen:  # a cycle in the tree would recurse forever
                continue
            path = f"{prefix}/{names[atlas.id]}"
            out[atlas.id] = path
            walk(atlas.id, path, seen | {atlas.id})

    walk(None, root, frozenset())
    return out


def asset_dirs(db: Session, project_id: int) -> dict[int, str]:
    """Storage-relative folder for every asset in the project, keyed by asset id."""
    domains = domain_dirs(db, project_id)
    assets = db.scalars(select(Asset).where(Asset.project_id == project_id)).all()
    grouped: dict[str, list[Asset]] = {}
    for asset in assets:
        # An asset pointing at a domain from another project would have no entry.
        parent = domains.get(asset.atlas_id, domains[None])
        grouped.setdefault(parent, []).append(asset)

    out: dict[int, str] = {}
    for parent, group in grouped.items():
        names = _dedupe({a.id: safe_name(a.name) for a in group})
        for asset in group:
            out[asset.id] = f"{parent}/{names[asset.id]}"
    return out


def asset_dir(db: Session, asset: Asset) -> Path:
    """Absolute folder for one asset, created if missing."""
    rel = asset_dirs(db, asset.project_id).get(asset.id)
    if rel is None:  # not yet flushed to the DB — derive it directly
        domains = domain_dirs(db, asset.project_id)
        parent = domains.get(asset.atlas_id, domains[None])
        rel = f"{parent}/{safe_name(asset.name)}"
    path = STORAGE_DIR / rel
    path.mkdir(parents=True, exist_ok=True)
    return path


def mockup_dir(project_id: int, mockup_id: int) -> Path:
    path = STORAGE_DIR / f"projects/{project_id}/{MOCKUPS_DIR}/{mockup_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_subdir(project_id: int, kind: str) -> Path:
    path = STORAGE_DIR / f"projects/{project_id}/{kind}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- ownership


def _norm(rel: str | None) -> str | None:
    """DB paths are storage-relative POSIX strings; compare them one way only."""
    if not rel:
        return None
    return str(rel).replace("\\", "/").strip("/") or None


def owners(db: Session, project_id: int) -> dict[str, str]:
    """Every storage path this project records, mapped to the folder it belongs in.

    A path can be referenced by several rows (an earlier version's PNG is commonly the
    reference image a later version was redrawn from), so ownership is claimed in
    priority order and the first claim wins: whoever *produced* the file owns it, and
    everyone else merely points at it.
    """
    out: dict[str, str] = {}
    a_dirs = asset_dirs(db, project_id)

    def claim(rel: str | None, folder: str) -> None:
        key = _norm(rel)
        if key and key not in out:
            out[key] = folder

    assets = db.scalars(select(Asset).where(Asset.project_id == project_id)).all()
    asset_ids = [a.id for a in assets]
    versions = (
        db.scalars(select(AssetVersion).where(AssetVersion.asset_id.in_(asset_ids))).all()
        if asset_ids else []
    )
    mockups = db.scalars(select(Mockup).where(Mockup.project_id == project_id)).all()
    project = db.get(Project, project_id)

    # 1. The images a version produced.
    for version in versions:
        folder = a_dirs.get(version.asset_id)
        if folder:
            claim(version.raw_path, folder)
            claim(version.processed_path, folder)
    # 2. Mockup screenshots.
    for mockup in mockups:
        claim(mockup.image_path, f"projects/{project_id}/{MOCKUPS_DIR}/{mockup.id}")
    # 3. Whatever is left that a version or asset points at — crops cut from a mockup,
    #    uploaded references. These belong with the asset that uses them.
    for version in versions:
        folder = a_dirs.get(version.asset_id)
        if folder:
            for rel in version.reference_paths or []:
                claim(rel, folder)
    for asset in assets:
        folder = a_dirs.get(asset.id)
        if folder:
            for rel in asset.reference_images or []:
                claim(rel, folder)
    # 4. Project-level style references.
    if project:
        for rel in project.reference_images or []:
            claim(rel, f"projects/{project_id}/{REFS_DIR}")
    return out


def _free_path(folder: Path, filename: str) -> Path:
    """`folder/filename`, suffixed if something already sits there."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, dot, ext = filename.partition(".")
    for n in range(2, 1000):
        candidate = folder / f"{stem}-{n}{dot}{ext}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{int(time.time() * 1000)}{dot}{ext}"


def _rewrite_paths(db: Session, project_id: int, moves: dict[str, str]) -> None:
    """Point every DB path field at where its file actually ended up."""
    if not moves:
        return

    def fix(rel: str | None) -> str | None:
        key = _norm(rel)
        return moves.get(key, rel) if key else rel

    assets = db.scalars(select(Asset).where(Asset.project_id == project_id)).all()
    asset_ids = [a.id for a in assets]
    for asset in assets:
        refs = [fix(r) for r in (asset.reference_images or [])]
        if refs != (asset.reference_images or []):
            asset.reference_images = refs
    if asset_ids:
        for version in db.scalars(
            select(AssetVersion).where(AssetVersion.asset_id.in_(asset_ids))
        ).all():
            version.raw_path = fix(version.raw_path) or ""
            version.processed_path = fix(version.processed_path) or ""
            refs = [fix(r) for r in (version.reference_paths or [])]
            if refs != (version.reference_paths or []):
                version.reference_paths = refs
    for mockup in db.scalars(select(Mockup).where(Mockup.project_id == project_id)).all():
        mockup.image_path = fix(mockup.image_path) or ""
    project = db.get(Project, project_id)
    if project:
        refs = [fix(r) for r in (project.reference_images or [])]
        if refs != (project.reference_images or []):
            project.reference_images = refs


def reconcile(db: Session, project_id: int, *, commit: bool = True) -> dict:
    """Move every recorded file into the folder it now belongs in and fix up the DB.

    Called after anything that changes where a file *should* live — an asset moved to
    another domain, a domain renamed, reparented or deleted. Idempotent: with nothing
    misplaced it touches no files.
    """
    moved: dict[str, str] = {}
    missing: list[str] = []
    for rel, folder in owners(db, project_id).items():
        src = STORAGE_DIR / rel
        if not src.exists():
            missing.append(rel)
            continue
        dest_dir = STORAGE_DIR / folder
        if src.parent.resolve() == dest_dir.resolve():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = _free_path(dest_dir, src.name)
        shutil.move(str(src), str(dest))
        moved[rel] = dest.relative_to(STORAGE_DIR).as_posix()

    _rewrite_paths(db, project_id, moved)
    if commit:
        db.commit()
    remove_empty_dirs(STORAGE_DIR / f"projects/{project_id}/{DOMAINS_DIR}")
    return {"moved": moved, "missing": missing}


# ----------------------------------------------------------------------------- removal


def _inside_storage(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    projects_root = (STORAGE_DIR / "projects").resolve()
    if resolved == projects_root or not resolved.is_relative_to(projects_root):
        return False
    # `projects/<pid>` itself is only removable through delete_project, which calls
    # `remove_project_dir` — everything else must be at least one level deeper.
    return len(resolved.relative_to(projects_root).parts) >= 2


def remove_dir(path: Path) -> bool:
    """Delete a folder and everything under it, refusing anything outside a project.

    The guard is the point: these calls are driven by names and ids coming out of the DB,
    and a bug that produced an empty or absolute path would otherwise hand `rmtree` the
    whole storage tree.
    """
    if not path or not _inside_storage(path) or not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def remove_file(rel: str | None) -> bool:
    """Delete one storage-relative file, refusing anything outside a project."""
    key = _norm(rel)
    if not key:
        return False
    path = STORAGE_DIR / key
    if not _inside_storage(path) or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def remove_project_dir(project_id: int) -> bool:
    path = (STORAGE_DIR / "projects" / str(project_id)).resolve()
    projects_root = (STORAGE_DIR / "projects").resolve()
    if not path.is_relative_to(projects_root) or path == projects_root or not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def remove_empty_dirs(root: Path) -> None:
    """Prune directories left empty behind a move or a delete. `root` itself is kept."""
    projects_root = (STORAGE_DIR / "projects").resolve()
    if not root.exists() or not root.resolve().is_relative_to(projects_root):
        return
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def prune_orphans(db: Session, project_id: int, *, dry_run: bool = True) -> dict:
    """Delete files under the project that no DB row points at any more.

    `runs/` is exempt: a run folder's log references only images inside itself, and it is
    the history of how an asset was made rather than part of the asset.
    """
    project_root = STORAGE_DIR / "projects" / str(project_id)
    if not project_root.exists():
        return {"deleted": [], "bytes": 0, "kept": 0}

    keep = set(owners(db, project_id))
    runs_root = (project_root / RUNS_DIR).resolve()
    deleted: list[str] = []
    freed = 0
    for path in project_root.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve().is_relative_to(runs_root):
            continue
        rel = path.relative_to(STORAGE_DIR).as_posix()
        if rel in keep:
            continue
        freed += path.stat().st_size
        deleted.append(rel)
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                pass
    if not dry_run:
        remove_empty_dirs(project_root)
    return {"deleted": deleted, "bytes": freed, "kept": len(keep), "dry_run": dry_run}
