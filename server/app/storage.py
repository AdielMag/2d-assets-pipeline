"""Helpers for laying out generated/uploaded files under the repo storage/ dir.

All DB image paths are stored relative to STORAGE_DIR and served at /storage/<path>.

An asset's own files go in the asset's own folder, mirroring the domain tree — see
`layout` for the shape and for the reconcile/delete side of it. `new_image_path` remains
for the handful of things that belong to a *project* rather than an asset (mockup
screenshots, throwaway previews, project style references).
"""
import re
import time
from pathlib import Path

from sqlalchemy.orm import Session

from . import layout
from .config import STORAGE_DIR
from .models import Asset


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "unnamed"


def project_dir(project_id: int) -> Path:
    d = STORAGE_DIR / "projects" / str(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_image_path(project_id: int, kind: str, name: str, ext: str = "png") -> Path:
    """A project-level file: kind is `mockups`, `previews`, `refs` or `_work`.

    Asset images do NOT belong here — use `new_asset_path`, which files them under the
    asset's own folder so they can be moved and deleted with it.
    """
    d = project_dir(project_id) / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slugify(name)}-{int(time.time() * 1000)}.{ext}"


def new_asset_path(db: Session, asset: Asset, name: str, ext: str = "png") -> Path:
    """A path inside `asset`'s own folder that doesn't exist yet."""
    return layout.asset_dir(db, asset) / f"{slugify(name)}-{int(time.time() * 1000)}.{ext}"


def new_mockup_path(project_id: int, mockup_id: int, name: str, ext: str = "png") -> Path:
    """A path inside one mockup's folder — its screenshot, or a crop taken from it."""
    return layout.mockup_dir(project_id, mockup_id) / f"{slugify(name)}-{int(time.time() * 1000)}.{ext}"


def new_work_path(project_id: int, name: str, ext: str = "png") -> Path:
    """A throwaway intermediate nothing will record in the DB (see layout.EPHEMERAL_DIRS)."""
    return new_image_path(project_id, layout.WORK_DIR, name, ext)


def rel_path(abs_path: Path) -> str:
    return abs_path.relative_to(STORAGE_DIR).as_posix()


def abs_path(rel: str) -> Path:
    return STORAGE_DIR / rel
