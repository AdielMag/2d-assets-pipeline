from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typing import Literal

from .. import layout
from ..config import STORAGE_DIR
from ..db import get_db
from ..dialogs import pick_folder
from ..models import Asset, Atlas, MockupRegion
from ..schemas import AtlasCreate, AtlasOut, AtlasUpdate
from ..unity_export import ExportError, domain_folder_from_pick, resolve_atlas_parent, sprites_dir
from .projects import get_project_or_404

router = APIRouter(prefix="/api", tags=["atlases"])


def get_atlas_or_404(db: Session, atlas_id: int) -> Atlas:
    atlas = db.get(Atlas, atlas_id)
    if not atlas:
        raise HTTPException(404, "Atlas not found")
    return atlas


def _with_count(db: Session, atlas: Atlas) -> AtlasOut:
    n = len(db.scalars(select(Asset.id).where(Asset.atlas_id == atlas.id)).all())
    out = AtlasOut.model_validate(atlas)
    out.asset_count = n
    return out


def ancestors_of(db: Session, atlas: Atlas) -> list[Atlas]:
    """Chain from this atlas's parent up to the root, closest first."""
    chain = []
    seen = {atlas.id}
    current = atlas
    while current.parent_id is not None:
        parent = db.get(Atlas, current.parent_id)
        if parent is None or parent.id in seen:
            break
        chain.append(parent)
        seen.add(parent.id)
        current = parent
    return chain


def available_assets(db: Session, atlas: Atlas) -> list[Asset]:
    """Assets usable from this domain: its own plus every ancestor's (reuse, no dup)."""
    ids = [atlas.id] + [a.id for a in ancestors_of(db, atlas)]
    return db.scalars(
        select(Asset).where(Asset.atlas_id.in_(ids), Asset.selected_version_id.is_not(None))
    ).all()


def _would_cycle(db: Session, atlas_id: int, new_parent_id: int | None) -> bool:
    node_id = new_parent_id
    seen = set()
    while node_id is not None:
        if node_id == atlas_id:
            return True
        if node_id in seen:
            break
        seen.add(node_id)
        parent = db.get(Atlas, node_id)
        node_id = parent.parent_id if parent else None
    return False


@router.get("/projects/{project_id}/atlases", response_model=list[AtlasOut])
def list_atlases(project_id: int, db: Session = Depends(get_db)):
    get_project_or_404(db, project_id)
    rows = db.scalars(
        select(Atlas).where(Atlas.project_id == project_id).order_by(Atlas.name)
    ).all()
    return [_with_count(db, a) for a in rows]


@router.post("/projects/{project_id}/atlases", response_model=AtlasOut)
def create_atlas(project_id: int, body: AtlasCreate, db: Session = Depends(get_db)):
    get_project_or_404(db, project_id)
    if body.parent_id is not None:
        parent = get_atlas_or_404(db, body.parent_id)
        if parent.project_id != project_id:
            raise HTTPException(400, "Parent atlas belongs to a different project")
    atlas = Atlas(project_id=project_id, name=body.name, parent_id=body.parent_id)
    db.add(atlas)
    db.commit()
    # A new domain has no assets of its own, but if it shares a name with a sibling then
    # that sibling's folder just stopped being unique and has to pick up its id suffix.
    layout.reconcile(db, project_id)
    db.refresh(atlas)
    return _with_count(db, atlas)


@router.patch("/atlases/{atlas_id}", response_model=AtlasOut)
def update_atlas(atlas_id: int, body: AtlasUpdate, db: Session = Depends(get_db)):
    atlas = get_atlas_or_404(db, atlas_id)
    updates = body.model_dump(exclude_unset=True)
    if "parent_id" in updates:
        new_parent = updates["parent_id"]
        if new_parent is not None:
            parent = get_atlas_or_404(db, new_parent)
            if parent.project_id != atlas.project_id:
                raise HTTPException(400, "Parent atlas belongs to a different project")
            if _would_cycle(db, atlas.id, new_parent):
                raise HTTPException(400, "That would create a cycle in the domain tree")
    relocating = any(
        field in updates and updates[field] != getattr(atlas, field)
        for field in ("name", "parent_id")
    )
    for field, value in updates.items():
        setattr(atlas, field, value)
    db.commit()
    if relocating:
        # Every asset at or below this domain now belongs at a different path.
        layout.reconcile(db, atlas.project_id)
        db.refresh(atlas)
    return _with_count(db, atlas)


@router.post("/atlases/{atlas_id}/browse-export-path")
def browse_atlas_export_path(atlas_id: int, db: Session = Depends(get_db)):
    atlas = get_atlas_or_404(db, atlas_id)
    project = get_project_or_404(db, atlas.project_id)
    try:
        current = resolve_atlas_parent(project, atlas)  # e.g. "Sprites" or "Sprites/UI"
        initial = sprites_dir(project) / current.removeprefix("Sprites").lstrip("/")
        initial.mkdir(parents=True, exist_ok=True)
        picked = pick_folder(str(initial))
        if picked is None:
            return {"path": None}
        folder = domain_folder_from_pick(project, picked)
    except ExportError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Could not open folder picker: {e}")
    return {"path": folder}


def _get_descendant_atlas_ids(db: Session, atlas_id: int) -> list[int]:
    all_atlases = db.scalars(select(Atlas)).all()
    children = {}
    for a in all_atlases:
        children.setdefault(a.parent_id, []).append(a.id)
    out = []
    def dfs(node_id):
        out.append(node_id)
        for c in children.get(node_id, []):
            dfs(c)
    dfs(atlas_id)
    return out


@router.delete("/atlases/{atlas_id}")
def delete_atlas(atlas_id: int, mode: str = "only", cascade: bool = False, db: Session = Depends(get_db)):
    print(f"DEBUG: atlas_id={atlas_id} mode={mode} cascade={cascade}")
    if cascade:
        mode = "cascade"
        
    atlas = get_atlas_or_404(db, atlas_id)
    project_id = atlas.project_id
    domain_folder = STORAGE_DIR / layout.domain_dirs(db, project_id)[atlas_id]
    if mode in ("cascade", "content_only"):
        descendant_ids = _get_descendant_atlas_ids(db, atlas_id)
        assets = db.scalars(select(Asset).where(Asset.atlas_id.in_(descendant_ids))).all()
        asset_ids = [a.id for a in assets]
        # Same reason as delete_asset: the folders are derived from names and the domain
        # chain, so they have to be resolved while the rows still exist.
        asset_folders = [layout.asset_dir(db, a) for a in assets]
        if asset_ids:
            db.execute(
                update(MockupRegion)
                .where(MockupRegion.asset_id.in_(asset_ids))
                .values(asset_id=None)
            )
            db.execute(
                update(MockupRegion)
                .where(MockupRegion.icon_asset_id.in_(asset_ids))
                .values(icon_asset_id=None)
            )
            for asset in assets:
                db.delete(asset)
                
        atlases_to_delete_ids = descendant_ids if mode == "cascade" else [i for i in descendant_ids if i != atlas_id]
        if atlases_to_delete_ids:
            atlases_to_delete = db.scalars(select(Atlas).where(Atlas.id.in_(atlases_to_delete_ids))).all()
            for a in atlases_to_delete:
                a.parent_id = None
            db.flush()
            for a in atlases_to_delete:
                db.delete(a)
        db.commit()
        for folder in asset_folders:
            layout.remove_dir(folder)
    else:
        # Reparent children up to this node's parent, and unassign its direct assets,
        # rather than cascading — deleting a domain shouldn't delete generated art.
        for child in db.scalars(select(Atlas).where(Atlas.parent_id == atlas.id)).all():
            child.parent_id = atlas.parent_id
        for asset in db.scalars(select(Asset).where(Asset.atlas_id == atlas.id)).all():
            asset.atlas_id = None
        db.delete(atlas)
        db.commit()
    if mode == "cascade":
        layout.remove_dir(domain_folder)
    # Whatever survived this domain — reparented children, unassigned assets, or in
    # content_only mode the domain itself — now lives at a different path. Runs after the
    # removal above so it can never move a survivor into the folder about to be deleted.
    layout.reconcile(db, project_id)
    layout.remove_empty_dirs(STORAGE_DIR / f"projects/{project_id}/{layout.DOMAINS_DIR}")
    return {"ok": True, "debug_mode": mode, "debug_cascade": cascade}


@router.get("/atlases/{atlas_id}/available-assets", response_model=list)
def list_available_assets(atlas_id: int, db: Session = Depends(get_db)):
    from ..schemas import AssetOut

    atlas = get_atlas_or_404(db, atlas_id)
    return [AssetOut.model_validate(a) for a in available_assets(db, atlas)]
