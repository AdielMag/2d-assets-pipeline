from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ASSET_TYPES = ("ui_element", "icon", "sprite", "tile", "sprite_sheet")

# Defaults for newly created assets that don't specify their own.
DEFAULT_ASPECT_RATIO = "1:1"  # matches the providers' default generation size, 1024x1024
DEFAULT_RESOLUTION = "256x256"  # small/fast by default; use /assets/{id}/upscale to grow

# Default subfolder (relative to Assets/Sprites/) each asset type exports into.
# "sprite" is "" because it exports straight into Assets/Sprites/ itself — every domain
# is forced under that root; see unity_export.resolve_domain_folder for the override +
# nesting logic, and Project.type_folders for the per-project overrides.
TYPE_FOLDER_DEFAULTS = {
    "ui_element": "UI",
    "icon": "Icons",
    "sprite": "",
    "tile": "Tiles",
    "sprite_sheet": "SpriteSheets",
}


class ProjectCreate(BaseModel):
    name: str
    style_description: str = ""
    palette: list[str] = []


class ProjectUpdate(BaseModel):
    name: str | None = None
    style_description: str | None = None
    palette: list[str] | None = None
    unity_path: str | None = None
    ppu: int | None = None
    filter_mode: str | None = None
    wrap_mode: str | None = None
    power_of_two: bool | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    style_description: str
    palette: list
    reference_images: list
    unity_path: str
    ppu: int
    filter_mode: str
    wrap_mode: str
    power_of_two: bool
    created_at: datetime
    updated_at: datetime
    asset_count: int = 0


class AtlasCreate(BaseModel):
    name: str
    parent_id: int | None = None


class AtlasUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    export_path: str | None = None


class AtlasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    parent_id: int | None
    export_path: str | None = None
    created_at: datetime
    updated_at: datetime
    asset_count: int = 0


class AssetCreate(BaseModel):
    name: str
    type: str
    prompt: str = ""
    atlas_id: int | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    is_sliced: bool | None = None
    override_entire_prompt: bool = False
    prompt_mode: str = "generate"
    reference_ops: list[str] = []


class AssetUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    prompt: str | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    nine_slice: dict | None = None
    sheet_rows: int | None = None
    sheet_cols: int | None = None
    ppu_override: int | None = None
    selected_version_id: int | None = None
    atlas_id: int | None = None
    reference_images: list[str] | None = None
    override_entire_prompt: bool | None = None
    prompt_mode: str | None = None
    reference_ops: list[str] | None = None


class AssetVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    model: str | None = None
    composed_prompt: str
    reference_paths: list = []
    raw_path: str
    processed_path: str
    fidelity: dict | None = None
    created_at: datetime


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    atlas_id: int | None
    name: str
    type: str
    prompt: str
    aspect_ratio: str | None
    resolution: str | None
    nine_slice: dict | None
    sheet_rows: int | None
    sheet_cols: int | None
    ppu_override: int | None
    reference_images: list = []
    override_entire_prompt: bool = False
    prompt_mode: str = "generate"
    reference_ops: list = []
    selected_version_id: int | None
    fidelity: dict | None = None  # selected version's score; see processing/fidelity.py
    created_at: datetime
    updated_at: datetime
    versions: list[AssetVersionOut] = []


class RegionCreate(BaseModel):
    name: str
    x: float
    y: float
    w: float
    h: float
    color: str = "#6c8cff"
    prompt: str = ""
    asset_type: str = "ui_element"
    resolution: str | None = None


class RegionUpdate(BaseModel):
    name: str | None = None
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    color: str | None = None
    prompt: str | None = None
    asset_type: str | None = None
    resolution: str | None = None
    asset_id: int | None = None
    mirror: bool | None = None


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mockup_id: int
    name: str
    x: float
    y: float
    w: float
    h: float
    color: str
    prompt: str
    asset_type: str
    resolution: str | None = None
    asset_id: int | None
    icon_asset_id: int | None = None
    template: str | None = None
    icon_prompt: str | None = None
    mirror: bool = False
    source: str | None = None
    detect_rect: list | None = None


class LabelUpdate(BaseModel):
    text_mode: Literal["erase", "extract"] | None = None


class LabelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mockup_id: int
    name: str
    text: str
    x: float
    y: float
    w: float
    h: float
    color: str
    align: str
    text_mode: str | None = None


class MockupUpdate(BaseModel):
    name: str | None = None


class MockupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str = ""
    image_path: str
    prompt: str
    created_at: datetime
    regions: list[RegionOut] = []
    labels: list[LabelOut] = []
