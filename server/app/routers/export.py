from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..dialogs import pick_folder
from ..models import Asset
from ..unity_export import (
    ExportError,
    export_asset,
    export_atlases,
    export_screen_layout,
    import_settings_for,
    importer_installed,
    install_importer,
    pascal_name,
    resolve_domain_folder,
    resolve_export,
    screen_element,
)
from .projects import get_project_or_404

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/projects/{project_id}/export/status")
def export_status(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    return {
        "unity_path": project.unity_path,
        "importer_installed": importer_installed(project),
    }


@router.post("/projects/{project_id}/export/browse-unity-path")
def browse_unity_path(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    try:
        path = pick_folder(project.unity_path)
    except Exception as e:
        raise HTTPException(500, f"Could not open folder picker: {e}")
    return {"unity_path": path}


@router.post("/projects/{project_id}/export/install-importer")
def install(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    try:
        dest = install_importer(project)
    except ExportError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "path": str(dest)}


class ExportBody(BaseModel):
    asset_ids: list[int]


@router.post("/projects/{project_id}/export")
def export(project_id: int, body: ExportBody, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    results, errors = [], []
    for asset_id in body.asset_ids:
        asset = db.get(Asset, asset_id)
        if not asset or asset.project_id != project.id:
            errors.append({"asset_id": asset_id, "error": "Asset not found in project"})
            continue
        try:
            results.append(export_asset(project, asset))
        except ExportError as e:
            errors.append({"asset_id": asset_id, "error": str(e)})
    atlases = None
    try:
        atlases = export_atlases(project)
    except ExportError:
        pass
    return {"exported": results, "errors": errors, "atlases": atlases}


class Placement(BaseModel):
    asset_id: int
    x: float  # normalized 0..1, top-left origin
    y: float
    w: float
    h: float


class ScreenExportBody(BaseModel):
    name: str
    reference: str = "1080x1920"
    placements: list[Placement]


@router.post("/projects/{project_id}/export/screen")
def export_screen(project_id: int, body: ScreenExportBody, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    elements = []
    for z, p in enumerate(body.placements):
        asset = db.get(Asset, p.asset_id)
        if not asset or asset.project_id != project.id:
            raise HTTPException(400, f"Asset {p.asset_id} not found in project")
        elements.append(
            screen_element(project, asset, {"x": p.x, "y": p.y, "w": p.w, "h": p.h}, z)
        )
    try:
        return export_screen_layout(project, body.name, elements, body.reference)
    except ExportError as e:
        raise HTTPException(400, str(e))


@router.get("/assets/{asset_id}/import-settings")
def preview_import_settings(asset_id: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    project = get_project_or_404(db, asset.project_id)
    folder = resolve_domain_folder(project, asset.type)
    settings = import_settings_for(project, asset)
    settings, _, tw, th = resolve_export(project, asset, settings)
    return {
        "filename": f"{pascal_name(asset.name)}.import.json",
        "path": f"Assets/{folder}/{pascal_name(asset.name)}.png",
        "settings": settings,
        "size": [tw, th] if tw else None,
    }
