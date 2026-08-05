from datetime import datetime

from sqlalchemy import Boolean, JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    style_description: Mapped[str] = mapped_column(Text, default="")
    palette: Mapped[list] = mapped_column(JSON, default=list)  # ["#aabbcc", ...]
    reference_images: Mapped[list] = mapped_column(JSON, default=list)  # storage-relative paths
    unity_path: Mapped[str] = mapped_column(Text, default="")
    ppu: Mapped[int] = mapped_column(Integer, default=100)
    filter_mode: Mapped[str] = mapped_column(String(16), default="point")  # point | bilinear
    wrap_mode: Mapped[str] = mapped_column(String(16), default="clamp")  # clamp | repeat
    power_of_two: Mapped[bool] = mapped_column(Boolean, default=True)  # pad exports to POT
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    assets: Mapped[list["Asset"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    mockups: Mapped[list["Mockup"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    atlases: Mapped[list["Atlas"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Atlas(Base):
    """A domain/atlas node. Self-referential tree per project: a child domain can reuse
    any ancestor's assets, and the same node maps 1:1 to a Unity SpriteAtlas."""
    __tablename__ = "atlases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("atlases.id"), nullable=True)
    # Override for where this domain's own-named folder sits, relative to Assets/Sprites/
    # (e.g. "UI" puts it at Sprites/UI/<Name>). NULL uses the default parent ("Atlases").
    # The leaf folder is always the domain's own name — only its parent is overridable.
    # See unity_export.resolve_atlas_parent.
    export_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="atlases")
    assets: Mapped[list["Asset"]] = relationship(back_populates="atlas")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    atlas_id: Mapped[int | None] = mapped_column(ForeignKey("atlases.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(24))  # ui_element|icon|sprite|tile|sprite_sheet
    prompt: Mapped[str] = mapped_column(Text, default="")
    aspect_ratio: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "W:H", e.g. "1:1"
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "WxH" target export size, e.g. "256x256"
    nine_slice: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {l,t,r,b} px
    sheet_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_cols: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ppu_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_images: Mapped[list] = mapped_column(JSON, default=list)  # storage-relative paths
    # How this asset's pixels were produced: "extract" (segmented straight out of the
    # source screenshot — pixel-faithful, free, the default), "generate" (redrawn by an
    # image model — a fallback for elements too occluded to cut out) or "manual".
    source: Mapped[str] = mapped_column(String(16), default="extract")
    override_entire_prompt: Mapped[bool] = mapped_column(Boolean, default=False)
    # "generate" = free-text prompt; "reference" = prompt derived from ticked operations
    # (prompting.REFERENCE_OPS) that reproduce `reference_images` with specific edits.
    prompt_mode: Mapped[str] = mapped_column(String(16), default="generate")
    reference_ops: Mapped[list] = mapped_column(JSON, default=list)  # REFERENCE_OPS keys
    selected_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="assets")
    atlas: Mapped[Atlas | None] = relationship(back_populates="assets")
    versions: Mapped[list["AssetVersion"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )

    @property
    def selected_version(self) -> "AssetVersion | None":
        """The version this asset currently exports/previews. Falls back to the newest
        when nothing is explicitly selected, matching `unity_export._version_path`."""
        if self.selected_version_id is not None:
            for v in self.versions:
                if v.id == self.selected_version_id:
                    return v
        return self.versions[-1] if self.versions else None

    @property
    def fidelity(self) -> dict | None:
        """Selected version's fidelity score, surfaced on the asset for list views."""
        version = self.selected_version
        return version.fidelity if version else None


class AssetVersion(Base):
    __tablename__ = "asset_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    provider: Mapped[str] = mapped_column(String(24))  # antigravity | higgsfield
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. gpt-image-1, gemini-3.6-flash-high
    composed_prompt: Mapped[str] = mapped_column(Text, default="")
    reference_paths: Mapped[list] = mapped_column(JSON, default=list)  # storage-relative reference paths
    raw_path: Mapped[str] = mapped_column(Text, default="")  # storage-relative
    processed_path: Mapped[str] = mapped_column(Text, default="")  # storage-relative
    # {delta_e, ssim, alpha_iou, coverage, score} vs. the source crop this asset came
    # from — see processing/fidelity.py. Lives on the *version*, not the asset, so an
    # extracted v2 can be compared against a generated v1 of the same element.
    fidelity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="versions")


class Mockup(Base):
    __tablename__ = "mockups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # User-editable label shown in place of the "Screen N" fallback, and used as the
    # default Unity screen/prefab name on export (see export_mockup_screen).
    name: Mapped[str] = mapped_column(String(120), default="")
    image_path: Mapped[str] = mapped_column(Text, default="")  # storage-relative
    prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="mockups")
    regions: Mapped[list["MockupRegion"]] = relationship(
        back_populates="mockup", cascade="all, delete-orphan"
    )
    labels: Mapped[list["MockupLabel"]] = relationship(
        back_populates="mockup", cascade="all, delete-orphan"
    )


class MockupLabel(Base):
    """A run of text on the screen — a button caption, a currency amount, a player name.

    Text is deliberately NOT a sprite. Baking lettering into an extracted frame makes the
    frame single-purpose (a PLAY button that can only ever say PLAY) and looks wrong the
    moment it is scaled or 9-sliced, because the glyphs stretch with the frame. Recording
    the text as data instead lets two things happen: the lettering is inpainted off the
    frame so the sprite comes out blank, and Unity re-renders it with a real font at the
    right size, which is also what makes it localisable.
    """

    __tablename__ = "mockup_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mockup_id: Mapped[int] = mapped_column(ForeignKey("mockups.id"))
    name: Mapped[str] = mapped_column(String(120), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    # percentages of the mockup image (0-100), same convention as MockupRegion
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    color: Mapped[str] = mapped_column(String(16), default="#FFFFFF")
    align: Mapped[str] = mapped_column(String(12), default="center")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # User's per-label Keep/Remove/Extract choice from the Text step. NULL keeps the
    # lettering baked into its parent's frame untouched. "erase" lifts it off (survives
    # only as this row, for Unity to re-render with a real font). "extract" also lifts it
    # off, but redraws it as its own sprite layered back over the parent instead of
    # discarding it — see MockupRegion's old remove_text/extract_text comment, which this
    # supersedes: the choice is per caption, not per element, since one element (a nav
    # bar) commonly carries several independent captions that don't all want the same fate.
    text_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)

    mockup: Mapped[Mockup] = relationship(back_populates="labels")


class MockupRegion(Base):
    __tablename__ = "mockup_regions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mockup_id: Mapped[int] = mapped_column(ForeignKey("mockups.id"))
    name: Mapped[str] = mapped_column(String(120))
    # percentages of the mockup image (0-100), matching the design's region model
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    w: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    color: Mapped[str] = mapped_column(String(16), default="#6c8cff")
    prompt: Mapped[str] = mapped_column(Text, default="")
    asset_type: Mapped[str] = mapped_column(String(24), default="ui_element")
    # The rect as the detector first proposed it, [x, y, w, h] in the same percentages.
    # Extraction is allowed to widen a box that was clipping its element, but it must
    # always grow from THIS, never from the last grown result: re-running a build would
    # otherwise grow the already-grown box again, and boxes creep outward a little on
    # every rebuild until they swallow their neighbours.
    detect_rect: Mapped[list | None] = mapped_column(JSON, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "WxH" calculated by LLM/system
    asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Shared-background grouping: regions that are the same repeated component differing
    # only by an inner icon (e.g. gem vs gold currency pills) get an identical `template`
    # name; `icon_prompt` describes this instance's distinguishing icon. At build time the
    # frame is generated ONCE for the group (bound to `asset_id`, so every member shares the
    # SAME background asset) and each member's distinguishing icon is generated as its own
    # asset (bound to `icon_asset_id`). Nothing is baked into a combined sprite: the preview
    # and export layer the shared background + the per-member icon at render time, so the
    # atlas stores one background + N icons instead of N near-duplicate combined images.
    icon_asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template: Mapped[str | None] = mapped_column(String(120), nullable=True)
    icon_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when this region's crop is a horizontal mirror of another region's (e.g. a
    # "next"/"previous" arrow pair) — see _group_by_appearance. Rather than generating a
    # second near-duplicate asset, the region reuses the OTHER region's asset_id and gets
    # flipped at render/composite time.
    mirror: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-region override of how to produce this element ("extract" | "generate").
    # NULL means follow whatever mode the build run was started with.
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Set when the box was hand-edited since its asset was last built, cleared once that
    # rebuild lands. Distinguishes "just unbound by an edit" from "never had an asset" —
    # without it, build-atlas's reuse-by-name step matches the region right back onto the
    # very asset the edit was meant to replace (same name, still sitting in the atlas at
    # its old size), so a resized/moved box silently reverted on the next build. See
    # update_region and _build_atlas.
    force_rebuild: Mapped[bool] = mapped_column(Boolean, default=False)
    # Deprecated: the Text step's Keep/Remove/Extract choice used to live here, one choice
    # for every caption on the element. It is now per-caption — see
    # `MockupLabel.text_mode` — since one element (a nav bar) commonly carries several
    # independent captions that don't all want the same fate. Columns kept (unused) rather
    # than dropped, since SQLite ALTER TABLE can't drop columns without a table rebuild.
    remove_text: Mapped[bool] = mapped_column(Boolean, default=False)
    extract_text: Mapped[bool] = mapped_column(Boolean, default=False)

    mockup: Mapped[Mockup] = relationship(back_populates="regions")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
