"""Writes exported PNGs + .import.json sidecars into the Unity project, and installs
the AssetPipelineImporter editor script that applies the settings inside Unity."""
import json
import re
import shutil
import uuid
from pathlib import Path

from PIL import Image, PngImagePlugin

from .config import UNITY_TEMPLATE_DIR
from .models import Asset, Atlas, Project
from .schemas import TYPE_FOLDER_DEFAULTS
from .storage import abs_path

IMPORTER_FILENAME = "AssetPipelineImporter.cs"

# Minimal-but-valid Unity .meta stub for a PNG we want Unity to treat as a Sprite. The
# real per-asset import settings (sprite mode, 9-slice border, filter/wrap mode, PPU)
# are NOT encoded here — AssetPipelineImporter.cs's OnPreprocessTexture() sets those
# from the exported .import.json sidecar the moment Unity actually imports the file.
# This stub exists purely so we can assign — and keep stable across re-exports — the
# GUID that the hand-written .spriteatlas file below references, without waiting for
# Unity to import the file first (chicken-and-egg otherwise).
_TEXTURE_META_TEMPLATE = """fileFormatVersion: 2
guid: {guid}
TextureImporter:
  internalIDToNameTable: []
  externalObjects: {{}}
  serializedVersion: 11
  mipmaps:
    mipMapMode: 0
    enableMipMap: 0
    sRGBTexture: 1
    linearTexture: 0
  isReadable: 0
  textureFormat: 1
  maxTextureSize: 2048
  textureSettings:
    serializedVersion: 2
    filterMode: -1
    aniso: -1
    mipBias: -100
    wrapU: -1
    wrapV: -1
    wrapW: -1
  nPOTScale: 0
  spriteMode: 1
  spriteExtrude: 1
  spriteMeshType: 1
  alignment: 0
  spritePivot: {{x: 0.5, y: 0.5}}
  spritePixelsToUnits: 100
  spriteBorder: {{x: 0, y: 0, z: 0, w: 0}}
  alphaIsTransparency: 1
  spriteTessellationDetail: -1
  textureType: 8
  textureShape: 1
  platformSettings:
  - serializedVersion: 3
    buildTarget: DefaultTexturePlatform
    maxTextureSize: 2048
    resizeAlgorithm: 0
    textureFormat: -1
    textureCompression: 1
    compressionQuality: 50
    crunchedCompression: 0
    allowsAlphaSplitting: 0
    overridden: 0
  spriteSheet:
    serializedVersion: 2
    sprites: []
    outline: []
    physicsShape: []
    bones: []
  spritePackingTag:
  userData:
  assetBundleName:
  assetBundleVariant:
"""

# Best-effort SpriteAtlas (V2) asset serialization, based on the structure Unity itself
# writes when you build one via the Editor. UNVERIFIED against a real Unity install (none
# available in this environment) — if Unity rejects or silently ignores it, the
# auto-running SpriteAtlasBuilder.cs (see unity/Editor/) rebuilds it correctly via
# Unity's own SpriteAtlasAsset API the moment the project is opened, so this never
# requires a manual step even if this hand-written file needs correcting.
_SPRITE_ATLAS_TEMPLATE = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!687078895 &1
SpriteAtlas:
  m_ObjectHideFlags: 0
  m_CorrespondingSourceObject: {{fileID: 0, guid: 0000000000000000e000000000000000, type: 0}}
  m_PrefabInstance: {{fileID: 0}}
  m_PrefabAsset: {{fileID: 0}}
  m_Name: {name}
  m_EditorData:
    serializedVersion: 2
    textureSettings:
      serializedVersion: 2
      anisoLevel: 1
      compressionQuality: 50
      maxTextureSize: 2048
      textureCompression: 0
      filterMode: 1
      generateMipMaps: 0
      readable: 0
      crunchedCompression: 0
      sRGB: 1
    platformSettings: []
    packingSettings:
      serializedVersion: 2
      padding: 4
      blockOffset: 1
      allowAlphaSplitting: 0
      paddingPower: 1
      enableRotation: 0
      enableTightPacking: 0
      enableAlphaDilation: 0
    variantMultiplier: 1
    packables:
{packables}
    bindAsDefault: 1
  m_MasterAtlas: {{fileID: 0}}
  m_PackedSprites: []
  m_PackedSpriteNamesToIndex: []
  m_RenderDataMap: {{}}
  m_Tag: {name}
  m_IsVariant: 0
"""

# Types that may be padded to power-of-two. Tiles must keep their exact tileable size
# and sprite sheets their exact grid dimensions, so they are never padded.
POT_TYPES = {"ui_element", "icon", "sprite"}
MAX_TEXTURE = 8192


def _next_pot(n: int) -> int:
    p = 1
    while p < n and p < MAX_TEXTURE:
        p <<= 1
    return p


class ExportError(Exception):
    pass


def _unity_assets_dir(project: Project) -> Path:
    if not project.unity_path:
        raise ExportError("Unity project path is not set in Project Settings")
    root = Path(project.unity_path)
    if not root.exists():
        raise ExportError(f"Unity project path does not exist: {root}")
    assets = root / "Assets"
    assets.mkdir(exist_ok=True)
    return assets


def pascal_name(name: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", name)
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "Asset"


def importer_installed(project: Project) -> bool:
    try:
        assets = _unity_assets_dir(project)
    except ExportError:
        return False
    return (assets / "Editor" / IMPORTER_FILENAME).exists()


def install_importer(project: Project) -> Path:
    """Copy every pipeline Editor script (importer + screen builder) into the Unity
    project's Assets/Editor/. Returns the path of the importer script."""
    assets = _unity_assets_dir(project)
    editor_dir = assets / "Editor"
    editor_dir.mkdir(exist_ok=True)
    src_dir = UNITY_TEMPLATE_DIR / "Editor"
    for src in src_dir.glob("*.cs"):
        shutil.copyfile(src, editor_dir / src.name)
    return editor_dir / IMPORTER_FILENAME


def sprites_dir(project: Project) -> Path:
    """Assets/Sprites/, creating it if needed. Every domain folder — default or
    overridden — lives inside this root; see resolve_domain_folder."""
    path = _unity_assets_dir(project) / "Sprites"
    path.mkdir(exist_ok=True)
    return path


def domain_folder_from_pick(project: Project, picked: str) -> str:
    """Turn an absolute folder path (from the native OS picker) into a type_folders
    override value: its path relative to Assets/Sprites/. Raises ExportError if the
    pick falls outside that root — overrides may only rename/relocate the subfolder
    *within* Sprites/, never escape it."""
    root = sprites_dir(project).resolve()
    chosen = Path(picked).resolve()
    if chosen != root and root not in chosen.parents:
        raise ExportError("Pick a folder inside the project's Assets/Sprites/ folder")
    rel = chosen.relative_to(root).as_posix()
    return "" if rel == "." else rel


def atlas_folder_name(atlas_name: str) -> str:
    return pascal_name(atlas_name)


def resolve_atlas_parent(project: Project, atlas: Atlas) -> str:
    """Folder (relative to Assets/) that a domain's own `<AtlasName>/` folder sits
    inside. Every domain is forced under Sprites/ — atlas.export_path only lets you
    choose *where under it* the domain lives (default: Sprites/Atlases); the leaf
    folder itself is always named for the atlas, never overridable."""
    raw = atlas.export_path if atlas.export_path is not None else "Atlases"
    parts = _clean_subpath(raw)
    if parts and parts[0].lower() == "sprites":
        parts = parts[1:]
    return "/".join(["Sprites", *parts]) if parts else "Sprites"


def _clean_subpath(raw: str) -> list[str]:
    """Split a user-supplied folder override into safe path segments: drop empty
    segments, `.`/`..` (no escaping the Unity project), and normalize slash direction."""
    return [p for p in re.split(r"[\\/]+", raw.strip()) if p not in ("", ".", "..")]


def resolve_domain_folder(project: Project, asset_type: str) -> str:
    """Folder (relative to Assets/) a non-atlas (no domain assigned) asset's PNG lands
    in — its fixed type folder, nested under Assets/Sprites/. Not overridable; only a
    domain's own export path is (see resolve_atlas_parent) — an unassigned asset has no
    domain to attach a path to."""
    parts = _clean_subpath(TYPE_FOLDER_DEFAULTS.get(asset_type, ""))
    return "/".join(["Sprites", *parts]) if parts else "Sprites"


def asset_export_folder(project: Project, asset: Asset) -> str:
    """Folder (relative to Assets/) an asset's PNG lives in: its domain/atlas folder
    when it belongs to one (so Unity packs it into that atlas), else its (possibly
    overridden) type-based folder. Always nested under Assets/Sprites/."""
    if asset.atlas is not None:
        return f"{resolve_atlas_parent(project, asset.atlas)}/{atlas_folder_name(asset.atlas.name)}"
    return resolve_domain_folder(project, asset.type)


def ensure_meta_guid(png_path: Path) -> str:
    """Return the GUID Unity will use for this PNG, assigning one now (via a minimal
    .meta stub) if the file has never been imported yet. Reuses an existing .meta's
    GUID untouched — once Unity (or us) has assigned one, it must never change, or
    every existing reference to that asset (scenes, prefabs, this same atlas file on
    the next export) would silently break."""
    meta_path = png_path.parent / f"{png_path.name}.meta"
    if meta_path.exists():
        m = re.search(r"^guid:\s*([0-9a-f]{32})", meta_path.read_text(), re.M)
        if m:
            return m.group(1)
    guid = uuid.uuid4().hex
    meta_path.write_text(_TEXTURE_META_TEMPLATE.format(guid=guid))
    return guid


def build_native_atlas(atlas_name: str, folder: Path) -> Path | None:
    """Write <folder>.spriteatlas next to `folder`, packing every PNG directly inside
    it by GUID (assigning/reusing one .meta per PNG). Returns the .spriteatlas path, or
    None if the folder has no exported PNGs yet."""
    pngs = sorted(folder.glob("*.png"))
    if not pngs:
        return None
    packables = "\n".join(
        f"    - {{fileID: 2800000, guid: {ensure_meta_guid(p)}, type: 3}}" for p in pngs
    )
    atlas_path = folder.parent / f"{atlas_name}.spriteatlas"
    atlas_path.write_text(
        _SPRITE_ATLAS_TEMPLATE.format(name=atlas_name, packables=packables)
    )
    return atlas_path


def export_atlases(project: Project) -> dict:
    """Write Assets/Sprites/Atlases/atlases.json describing the domain tree, so the
    Unity-side editor script can (re)build one SpriteAtlas per domain deterministically.
    Also writes each domain's .spriteatlas file directly (best-effort — see
    _SPRITE_ATLAS_TEMPLATE) so the atlases exist with no manual Unity step; the
    auto-running SpriteAtlasBuilder.cs corrects them via Unity's own API as soon as the
    project is opened, regardless.

    Each domain's own folder can live anywhere under Sprites/ (atlas.export_path,
    default Sprites/Atlases) — but the manifest itself always lives at the default
    location, since that's the one fixed path the Unity-side script knows to read."""
    assets_dir = _unity_assets_dir(project)
    manifest_dir = sprites_dir(project) / "Atlases"
    manifest_dir.mkdir(exist_ok=True)

    built = []
    for atlas in project.atlases:
        name = atlas_folder_name(atlas.name)
        parent_dir = assets_dir / resolve_atlas_parent(project, atlas)
        parent_dir.mkdir(parents=True, exist_ok=True)
        result = build_native_atlas(name, parent_dir / name)
        if result is not None:
            built.append(name)
    by_id = {a.id: a for a in project.atlases}
    nodes = [
        {
            "name": atlas_folder_name(a.name),
            "parent": atlas_folder_name(by_id[a.parent_id].name) if a.parent_id in by_id else None,
            "folder": f"Assets/{resolve_atlas_parent(project, a)}/{atlas_folder_name(a.name)}",
        }
        for a in project.atlases
    ]
    path = manifest_dir / "atlases.json"
    path.write_text(json.dumps({"atlases": nodes}, indent=2))
    return {
        "path": f"Assets/{path.relative_to(assets_dir).as_posix()}",
        "count": len(nodes),
        "built_atlases": built,
    }


def import_settings_for(project: Project, asset: Asset) -> dict:
    is_sheet = asset.type == "sprite_sheet" and asset.sheet_rows and asset.sheet_cols
    sliced = asset.nine_slice is not None and asset.type not in ("tile", "sprite_sheet")
    ns = asset.nine_slice or {}
    return {
        "pixelsPerUnit": asset.ppu_override or project.ppu,
        # TextureImporter.spriteBorder order: left, bottom, right, top
        "spriteBorder": [ns.get("l", 0), ns.get("b", 0), ns.get("r", 0), ns.get("t", 0)] if sliced else None,
        "spriteMode": "Multiple" if is_sheet else ("Sliced" if sliced else "Single"),
        "filterMode": "Bilinear" if project.filter_mode == "bilinear" else "Point",
        "wrapMode": "Repeat" if (asset.type == "tile" or project.wrap_mode == "repeat") else "Clamp",
        "meshType": "FullRect" if (sliced or asset.type == "tile") else "Tight",
        "sheetRows": asset.sheet_rows or 0,
        "sheetCols": asset.sheet_cols or 0,
    }


def _version_path(asset: Asset) -> Path | None:
    version = next(
        (v for v in asset.versions if v.id == asset.selected_version_id),
        asset.versions[-1] if asset.versions else None,
    )
    if version is None:
        return None
    return abs_path(version.processed_path or version.raw_path)


def resolve_export(project: Project, asset: Asset, settings: dict) -> tuple[dict, "Image.Image | None", int, int]:
    """Given base import settings, apply power-of-two padding when enabled and
    applicable. Returns (settings, padded_image_or_None, out_width, out_height).
    padded_image is None when the source can be copied verbatim."""
    src = _version_path(asset)
    if src is None or not src.exists():
        return settings, None, 0, 0

    if not (project.power_of_two and asset.type in POT_TYPES):
        with Image.open(src) as im:
            return settings, None, im.width, im.height

    with Image.open(src) as im:
        im = im.convert("RGBA")
        w, h = im.size
        tw, th = _next_pot(w), _next_pot(h)
        if (tw, th) == (w, h):
            return settings, None, w, h
        px, py = tw - w, th - h
        pl, pt = px // 2, py // 2
        pr, pb = px - pl, py - pt
        canvas = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        canvas.paste(im, (pl, pt))

    border = settings.get("spriteBorder")
    if border:  # order is [left, bottom, right, top]
        settings = {
            **settings,
            "spriteBorder": [border[0] + pl, border[1] + pb, border[2] + pr, border[3] + pt],
        }
    return settings, canvas, tw, th


def screen_element(
    project: Project,
    asset: Asset,
    rect: dict,
    z: int,
    fit: str = "stretch",
    mirror: bool = False,
) -> dict:
    """One entry in a screen layout: which sprite goes where (normalized rect)."""
    folder = asset_export_folder(project, asset)
    name = pascal_name(asset.name)
    return {
        "kind": "sprite",
        "asset": name,
        "type": asset.type,
        "sprite": f"Assets/{folder}/{name}.png",
        "rect": {
            "x": round(float(rect["x"]), 5),
            "y": round(float(rect["y"]), 5),
            "w": round(float(rect["w"]), 5),
            "h": round(float(rect["h"]), 5),
        },
        "z": z,
        "fit": fit,
        "mirror": mirror,
    }


def label_element(rect: dict, name: str, text: str, color: str, align: str, z: int) -> dict:
    """One text entry in a screen layout — reconstructed as a TMP text object rather than
    a sprite (see MockupLabel's docstring for why text is kept as data, not pixels)."""
    return {
        "kind": "label",
        "asset": name,
        "text": text,
        "rect": {
            "x": round(float(rect["x"]), 5),
            "y": round(float(rect["y"]), 5),
            "w": round(float(rect["w"]), 5),
            "h": round(float(rect["h"]), 5),
        },
        "z": z,
        "color": color,
        "align": align,
    }


def export_screen_reference(project: Project, screen_name: str, src: Path) -> str:
    """Copy the mockup image the screen was built from into
    Assets/Screens/<Screen>.reference.png, so the original reference is available
    alongside the rebuilt prefab inside the Unity project. Not used by the prefab
    itself — Unity will import it as a plain Texture2D."""
    assets_dir = _unity_assets_dir(project)
    screens = assets_dir / "Screens"
    screens.mkdir(exist_ok=True)
    name = pascal_name(screen_name)
    dest = screens / f"{name}.reference.png"
    shutil.copyfile(src, dest)
    rel = dest.relative_to(assets_dir).as_posix()
    return f"Assets/{rel}"


def export_screen_layout(
    project: Project, screen_name: str, placements: list[dict], reference: str = "1080x1920"
) -> dict:
    """Write Assets/Screens/<Screen>.screen.json describing where each exported sprite
    sits on the screen. `placements` is a list of {asset, rect{x,y,w,h}} dicts already
    resolved to element form (see screen_element). Rects are normalized 0..1 with the
    origin at the top-left, matching the mockup region model."""
    assets_dir = _unity_assets_dir(project)
    screens = assets_dir / "Screens"
    screens.mkdir(exist_ok=True)
    name = pascal_name(screen_name)
    doc = {"screen": name, "reference": reference, "elements": placements}
    path = screens / f"{name}.screen.json"
    path.write_text(json.dumps(doc, indent=2))
    rel = path.relative_to(assets_dir).as_posix()
    return {"screen": name, "path": f"Assets/{rel}", "count": len(placements)}


def export_asset(project: Project, asset: Asset) -> dict:
    src = _version_path(asset)
    if src is None:
        raise ExportError(f"'{asset.name}' has no generated version")
    if not src.exists():
        raise ExportError(f"Image file missing for '{asset.name}'")

    assets_dir = _unity_assets_dir(project)
    folder = assets_dir / asset_export_folder(project, asset)
    folder.mkdir(parents=True, exist_ok=True)
    name = pascal_name(asset.name)

    settings = import_settings_for(project, asset)
    settings, padded, tw, th = resolve_export(project, asset, settings)

    # Embed the import settings as a PNG text chunk so the file's bytes change whenever
    # the 9-slice border / PPU / etc. changes — even when the pixels are identical. Unity
    # keys texture reimport off the PNG's content hash and ignores the sidecar JSON, so
    # without this a border-only re-export would never be picked up until a manual Reimport.
    meta = PngImagePlugin.PngInfo()
    meta.add_text("AssetPipeline", json.dumps(settings, sort_keys=True))

    png_path = folder / f"{name}.png"
    image = padded if padded is not None else Image.open(src)
    try:
        image.save(png_path, "PNG", pnginfo=meta)
    finally:
        if padded is None:
            image.close()

    json_path = folder / f"{name}.import.json"
    json_path.write_text(json.dumps(settings, indent=2))

    rel = png_path.relative_to(assets_dir).as_posix()
    return {"asset_id": asset.id, "path": f"Assets/{rel}", "settings": settings, "size": [tw, th]}
