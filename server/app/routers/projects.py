from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import layout
from ..db import get_db
from ..models import Asset, Project
from ..schemas import ProjectCreate, ProjectOut, ProjectUpdate
from ..storage import new_image_path, rel_path

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _with_count(db: Session, project: Project) -> ProjectOut:
    count = db.scalar(
        select(func.count(Asset.id)).where(Asset.project_id == project.id)
    )
    out = ProjectOut.model_validate(project)
    out.asset_count = count or 0
    return out


def get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    projects = db.scalars(select(Project).order_by(Project.updated_at.desc())).all()
    return [_with_count(db, p) for p in projects]


@router.post("", response_model=ProjectOut)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(
        name=body.name,
        style_description=body.style_description,
        palette=body.palette,
        reference_images=[],
    )
    db.add(project)
    db.commit()
    return _with_count(db, project)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)):
    return _with_count(db, get_project_or_404(db, project_id))


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    return _with_count(db, project)


@router.delete("/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    db.delete(project)
    db.commit()
    layout.remove_project_dir(project_id)
    return {"ok": True}


@router.post("/{project_id}/references", response_model=ProjectOut)
async def upload_reference(
    project_id: int, file: UploadFile, db: Session = Depends(get_db)
):
    project = get_project_or_404(db, project_id)
    ext = (file.filename or "ref.png").rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        raise HTTPException(400, "Unsupported image type")
    dest = new_image_path(project.id, "refs", "ref", ext)
    dest.write_bytes(await file.read())
    project.reference_images = [*project.reference_images, rel_path(dest)]
    db.commit()
    return _with_count(db, project)


@router.delete("/{project_id}/references", response_model=ProjectOut)
def remove_reference(project_id: int, path: str, db: Session = Depends(get_db)):
    project = get_project_or_404(db, project_id)
    project.reference_images = [p for p in project.reference_images if p != path]
    db.commit()
    if path not in layout.owners(db, project_id):
        layout.remove_file(path)
    return _with_count(db, project)
