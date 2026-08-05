from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy import text

from .config import STORAGE_DIR
from .db import Base, engine
from . import models  # noqa: F401 — register tables
from .routers import atlases, assets, export, generate, llm, mockups, processing, projects, providers, status

Base.metadata.create_all(engine)


def _migrate() -> None:
    """Add columns introduced after a DB was first created (SQLite create_all won't)."""
    added = {
        "projects": {"power_of_two": "BOOLEAN DEFAULT 1"},
        "atlases": {"export_path": "TEXT"},
        "mockups": {"name": "TEXT DEFAULT ''"},
        "assets": {"atlas_id": "INTEGER", "aspect_ratio": "TEXT", "resolution": "TEXT", "reference_images": "TEXT DEFAULT '[]'", "source": "TEXT", "override_entire_prompt": "BOOLEAN DEFAULT 0", "prompt_mode": "TEXT DEFAULT 'generate'", "reference_ops": "TEXT DEFAULT '[]'"},
        "mockup_regions": {"resolution": "TEXT", "template": "TEXT", "icon_prompt": "TEXT", "icon_asset_id": "INTEGER", "mirror": "BOOLEAN DEFAULT 0", "source": "TEXT", "detect_rect": "TEXT", "force_rebuild": "BOOLEAN DEFAULT 0", "remove_text": "BOOLEAN DEFAULT 0", "extract_text": "BOOLEAN DEFAULT 0"},
        "mockup_labels": {"text_mode": "TEXT"},
        "asset_versions": {"model": "TEXT", "reference_paths": "TEXT DEFAULT '[]'", "fidelity": "TEXT"},
    }
    with engine.begin() as conn:
        for table, cols in added.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

        # Any asset that predates the aspect_ratio feature (or was created before a
        # per-rect ratio could be inferred) still has NULL here — default it to 1:1,
        # matching the providers' own default generation size (1024x1024), so its
        # composed prompt always states a shape instead of leaving the model to guess.
        conn.execute(text("UPDATE assets SET aspect_ratio = '1:1' WHERE aspect_ratio IS NULL"))
        # Assets predating the two-mode prompt keep their free-text prompt behaviour.
        conn.execute(text("UPDATE assets SET prompt_mode = 'generate' WHERE prompt_mode IS NULL"))
        conn.execute(text("UPDATE assets SET reference_ops = '[]' WHERE reference_ops IS NULL"))
        # Every asset predating the extraction pipeline was produced by an image model.
        # Label them truthfully rather than letting the new 'extract' default imply they
        # were cut from a screenshot — the whole point of the column is to tell the two
        # apart when comparing fidelity.
        conn.execute(text("UPDATE assets SET source = 'generate' WHERE source IS NULL"))
        _backfill_resolutions(conn)


def _backfill_resolutions(conn) -> None:
    """Give every asset a `resolution` that matches the PNG it actually has.

    This used to blanket-set '256x256' for any NULL, which made the DB lie: ~20 assets
    predating the resolution feature have 1000-2200px PNGs while claiming 256x256.
    Everything that trusts the field — /upscale's doubling, export sizing, the UI — was
    then wrong for those rows. Read the real dimensions instead, and only fall back to
    the default when the file is missing."""
    from PIL import Image

    from .schemas import DEFAULT_RESOLUTION
    from .storage import abs_path

    rows = conn.execute(text(
        """
        SELECT a.id, a.resolution, v.processed_path, v.raw_path
        FROM assets a
        LEFT JOIN asset_versions v ON v.id = a.selected_version_id
        """
    )).fetchall()
    for asset_id, declared, processed, raw in rows:
        rel = processed or raw
        actual = None
        if rel:
            path = abs_path(rel)
            if path.exists():
                try:
                    with Image.open(path) as im:
                        actual = (im.width, im.height)
                except OSError:
                    pass

        if declared and actual:
            # `fit_to_resolution` scales an image to fit *inside* its declared box, so a
            # PNG strictly larger than the box proves the field was never real (it was
            # blanket-set by the old backfill). Anything that fits is left alone — a
            # legitimately downscaled asset is smaller than its box and must stay.
            try:
                dw, dh = (int(v) for v in declared.lower().split("x"))
            except ValueError:
                dw = dh = 0
            if actual[0] <= dw and actual[1] <= dh:
                continue
        elif declared:
            continue  # no file to check against; leave the declared value as-is

        resolution = f"{actual[0]}x{actual[1]}" if actual else DEFAULT_RESOLUTION
        conn.execute(
            text("UPDATE assets SET resolution = :res WHERE id = :id"),
            {"res": resolution, "id": asset_id},
        )


_migrate()

app = FastAPI(title="2D Assets Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(atlases.router)
app.include_router(assets.router)
app.include_router(generate.router)
app.include_router(processing.router)
app.include_router(mockups.router)
app.include_router(export.router)
app.include_router(llm.router)
app.include_router(providers.router)
app.include_router(status.router)

app.mount("/storage", StaticFiles(directory=STORAGE_DIR), name="storage")


@app.get("/api/health")
def health():
    return {"ok": True}
