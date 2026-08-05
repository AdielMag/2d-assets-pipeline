<div align="center">

# 🎨 2D Assets Pipeline

### Turn one game-UI screenshot into a library of clean, reusable Unity sprites.

<br/>

[![CI](https://github.com/AdielMag/2d-assets-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/AdielMag/2d-assets-pipeline/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/AdielMag/2d-assets-pipeline/branch/main/graph/badge.svg)](https://codecov.io/gh/AdielMag/2d-assets-pipeline)
[![Tests](https://img.shields.io/badge/tests-73%20passing-3fb950)](server/tests)
[![Patch coverage gate](https://img.shields.io/badge/patch%20coverage%20gate-80%25-6c8cff)](codecov.yml)

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev)
[![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white)](https://typescriptlang.org)
[![Vite](https://img.shields.io/badge/Vite-8-646CFF?logo=vite&logoColor=white)](https://vite.dev)
[![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)](https://sqlite.org)
[![Unity](https://img.shields.io/badge/Unity-SpriteAtlas-000000?logo=unity&logoColor=white)](https://unity.com)

</div>

![Element detection on a game lobby screen](docs/screenshots/07-elements.png)

---

### Overview

Feed it a screenshot → get back every button, frame, and icon as a separate transparent PNG, automatically imported into Unity as a sprite atlas.

**Why extract instead of generate?** A screenshot already contains the exact pixels of every element on it. The pipeline cuts elements out instead of asking an AI model to redraw them — keeping extraction pixel-perfect, free, and completely offline. Generative AI models serve as an optional fallback for heavily occluded elements.

```bash
# 1. Install
python -m venv server/.venv && server/.venv/Scripts/activate
pip install -r server/requirements.txt
npm --prefix client install

# 2. Run (both servers)
./start-dev.ps1
```

Then open **http://localhost:5173**. No API keys are required to start — extraction runs offline without calling any cloud providers.

<details>
<summary><b>📖 Table of Contents</b></summary>

<br/>

| Section | Contents |
|---|---|
| [🎯 How It Works](#-how-it-works) | Core segmentation strategy & comparison |
| [✨ Core Features](#-core-features) | Feature inventory & repository stats |
| [🚀 Quick Start](#-quick-start) | Step-by-step setup and running |
| [🏗️ Architecture](#️-architecture) | System diagram & tech stack details |
| [🔄 The 6-Step Pipeline](#-the-6-step-pipeline) | Breakdown workflow & visual guide |
| [🔬 Extraction Engine](#-extraction-engine) | HQ-SAM, alpha matting, and inpainting details |
| [🗃️ Data Model & Storage](#️-data-model--storage) | Schema ER diagram & disk directory structure |
| [🎮 Unity Integration](#-unity-integration) | SpriteAtlases, 9-slice, and prefab generation |
| [🧪 Testing & Quality Gates](#-testing--quality-gates) | Test suite and CI coverage enforcement |
| [🤝 Contributing](#-contributing) | Branch protection & merge requirements |

</details>

---

## 🎯 How It Works

By segmenting elements directly out of the screenshot rather than regenerating them, common extraction tasks are pixel-exact, free, and instant.

```mermaid
flowchart LR
    A["📱 Screenshot"] --> B["👁️ Vision LLM<br/>finds elements"]
    B --> C["📐 Edge snapping<br/><i>OpenCV + NMS</i>"]
    C --> D{"Occluded?"}
    D -->|"no · common case"| E["✂️ Segment & matte<br/><i>HQ-SAM → alpha matting</i>"]
    D -->|"yes"| F["🎨 Regenerate<br/><i>Gemini / Higgsfield</i>"]
    E --> G["🔤 Lift the lettering<br/><i>LaMa inpainting</i>"]
    F --> G
    G --> H["🖼️ Asset library"]
    H --> I["🎮 Unity<br/><i>SpriteAtlas + prefab</i>"]

    style A fill:#2a3550,stroke:#6c8cff,color:#eef0f2
    style E fill:#1f3d2b,stroke:#3fb950,color:#eef0f2
    style F fill:#3d2b1f,stroke:#f5b820,color:#eef0f2
    style I fill:#2b2233,stroke:#8a4fd6,color:#eef0f2
```

<details>
<summary><b>💡 Deep Dive: Why extract beats generate</b></summary>

<br/>

| Metric / Aspect | Extract (Cut from screenshot) | Generate (Image Model AI) |
|---|---|---|
| **Colour fidelity** | Exact — uses original pixels | Drifts in hue and saturation |
| **Shape fidelity** | Exact proportions | Plausible lookalike, wrong geometry |
| **Cost** | Free, offline | Burns provider quota / credits |
| **Speed** | Instant (seconds) | 10s–minutes per image |
| **Requirement** | Element is visible | None |
| **Primary Use** | Default path for UI elements | Heavily occluded elements only |

Both paths land in the same versioned asset system, allowing an extracted version to be scored directly against a generated version using built-in fidelity metrics.

</details>

---

## ✨ Core Features

- **🖼️ Asset Library**: Self-referential domain trees mapping 1:1 to Unity `SpriteAtlas` assets, complete version history, prompt tuning, non-destructive re-scaling, and interactive 9-slice editing.
- **🔍 Screen Breakdown Wizard**: Automatic Vision-LLM candidate detection, edge gradient snapping (OpenCV + NMS), sub-asset splitting, shared background grouping, and asset reuse matching.
- **🎨 Generation Providers**: Support for Antigravity (Google AI Pro/Ultra), Higgsfield CLI, and Gemini API with hard spend controls, pre-execution cost estimation, and automated chroma-keying.
- **🎮 Unity Export**: Automated `.spriteatlas` generation, `.import.json` sidecars, custom import scripts, 9-slice borders, and full screen prefab reconstruction.

<details>
<summary><b>📊 Repository Stats & Detailed Inventory</b></summary>

<br/>

### Repository at a Glance

| Area | Contents | Lines |
|---|---|---:|
| `server/app/` + `server/tools/` | FastAPI app, 10 routers, 12 image-processing modules, 4 providers, 13 CLI tools | **17,225** |
| `client/src/` | React 19 SPA — 7 pages, 6-step screen wizard, 8 shared components | **11,073** |
| `server/tests/` | 73 pytest tests over processing algorithms | **1,418** |
| `unity/Editor/` | Importer, atlas builder, screen layout builder | **566** |
| `unity-clashup/` | Demo Unity project with 60 exported sprites across 6 atlases | — |

<br/>

### Full Feature Breakdown

- **Asset Library**: Domain trees, version history per asset, custom prompts, aspect ratio, PPU, 9-slice controls, built-in image editor.
- **Screen Breakdown**: Vision-LLM element detection with per-edge snapping, sub-asset splitting, shared-background grouping, mirror detection (flipping symmetrical sprites), and live SSE progress.
- **Generation Providers**: Antigravity, Higgsfield, Gemini REST, per-provider spend-block toggles, live cost estimates, and magenta chroma-key removal.
- **Unity Export**: Automated `SpriteAtlas` creation, `.import.json` sidecars, point/bilinear filtering, clamp/repeat modes, power-of-two padding, and font-preserved text re-rendering.

</details>

---

## 🚀 Quick Start

### 1. Installation

```bash
git clone https://github.com/AdielMag/2d-assets-pipeline.git
cd 2d-assets-pipeline

# Server setup
python -m venv server/.venv
server/.venv/Scripts/activate  # On Windows
pip install -r server/requirements.txt

# Client setup
npm --prefix client install
```

### 2. Run the Application

```bash
# Windows launch script (starts server + client)
./start-dev.ps1
```

Access the UI at **http://localhost:5173** (API docs available at **http://localhost:8787/docs**).

<details>
<summary><b>⚙️ Provider Configuration & Optional Local ML Models</b></summary>

<br/>

### Environment & Providers Configuration

Copy `.env.example` to `server/.env`:
```bash
cp server/.env.example server/.env
```

Extraction requires zero keys. Optional generation provider credentials:
- **Antigravity**: Authenticated via Google AI Pro/Ultra login in the Antigravity CLI.
- **Higgsfield**: Configured via `@higgsfield/cli`.
- **Gemini**: Requires `GEMINI_API_KEY` set in `server/.env`.

Providers can be toggled on/off in the settings UI to prevent inadvertent API usage.

### Optional Local ML Acceleration

Installing PyTorch and local models improves extraction quality (falls back to classical algorithms if uninstalled):

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install --no-deps sam2 simple-lama-inpainting
pip install -r server/requirements-ml.txt
```

> **Note**: `--no-deps` on `sam2` prevents dependency conflicts with `opencv-python-headless`.

</details>

---

## 🏗️ Architecture

```mermaid
graph TB
    subgraph browser["🖥️ Browser — localhost:5173"]
        UI["React 19 + TypeScript<br/>react-router · SSE progress"]
    end

    subgraph server["⚙️ FastAPI — localhost:8787"]
        R["Routers<br/><i>projects · assets · atlases · generate<br/>mockups · processing · export · llm · providers</i>"]
        P["Processing<br/><i>extract · inpaint · transparency · nine_slice<br/>upres · fidelity · composite · region_detector</i>"]
        PR["Provider registry<br/><i>live probes + enable toggles</i>"]
        L["Prompting + LLM runner"]
    end

    subgraph data["💾 Local disk"]
        DB[("SQLite<br/>storage/app.db")]
        FS["storage/projects/…<br/><i>domain-mirrored tree</i>"]
    end

    subgraph ext["🌐 External Providers"]
        AG["Antigravity CLI"]
        HF["Higgsfield CLI"]
        GM["Gemini REST"]
        CL["Claude CLI"]
    end

    subgraph ml["🧠 Local ML Models (Optional)"]
        SAM["HQ-SAM / SAM2"]
        LAMA["LaMa inpainting"]
        ESR["Real-ESRGAN (ONNX)"]
    end

    UI -->|"REST + SSE"| R
    R --> P
    R --> PR
    R --> L
    P --> ml
    PR --> AG & HF & GM
    L --> CL & AG
    R --> DB
    R --> FS
    R -->|"PNG + .import.json"| U["🎮 Unity project"]

    style browser fill:#1a1d23,stroke:#6c8cff,color:#eef0f2
    style server fill:#1a1d23,stroke:#3fb950,color:#eef0f2
    style data fill:#1a1d23,stroke:#f5b820,color:#eef0f2
    style ext fill:#1a1d23,stroke:#8a4fd6,color:#eef0f2
    style ml fill:#1a1d23,stroke:#d63a3a,color:#eef0f2
    style U fill:#2b2233,stroke:#8a4fd6,color:#eef0f2
```

<details>
<summary><b>🛠️ Technology Stack Decisions & Rationale</b></summary>

<br/>

| Layer | Choice | Rationale |
|---|---|---|
| **API** | FastAPI + Uvicorn | Async SSE streaming for long generation tasks |
| **ORM** | SQLAlchemy 2.0 | Typed mappings (`Mapped[...]`), self-migrating schema |
| **Database** | SQLite | Zero-config, single-file local database |
| **Frontend** | React 19 + Vite 8 + TS | Hand-crafted modern dark-mode interface |
| **Core Imaging** | Pillow, OpenCV, NumPy, SciPy | Offline, deterministic processing primitives |
| **Segmentation** | HQ-SAM (`vit_tiny`), GrabCut | High-quality token preserves fine details and thin geometry |
| **Inpainting** | LaMa | Clean background restoration after lifting text/icons |
| **Upscaling** | Real-ESRGAN (ONNX) | Fast execution without heavy legacy dependencies |

</details>

---

## 🔄 The 6-Step Pipeline

```mermaid
flowchart LR
    S1["1️⃣ Screen"] --> S2["2️⃣ Elements"] --> S3["3️⃣ Build"] --> S4["4️⃣ Text"] --> S5["5️⃣ Polish"] --> S6["6️⃣ Result"]

    style S1 fill:#2a3550,stroke:#6c8cff,color:#eef0f2
    style S2 fill:#2a3550,stroke:#6c8cff,color:#eef0f2
    style S3 fill:#1f3d2b,stroke:#3fb950,color:#eef0f2
    style S4 fill:#3d2b1f,stroke:#f5b820,color:#eef0f2
    style S5 fill:#3d2b1f,stroke:#f5b820,color:#eef0f2
    style S6 fill:#2b2233,stroke:#8a4fd6,color:#eef0f2
```

| Step | Purpose |
|---|---|
| **1 · Screen** | Import or generate a screen mockup for analysis. |
| **2 · Elements** | Detect reusable UI elements and sub-components via Vision LLM + OpenCV. |
| **3 · Build** | Cut elements into assets, matching against existing domain items. |
| **4 · Text** | Choose whether to keep, erase, or extract text into re-renderable labels. |
| **5 · Polish** | Perform Real-ESRGAN upscaling and edge refitting. |
| **6 · Result** | Score reconstruction fidelity against the original screenshot and export to Unity. |

<details>
<summary><b>📸 Visual Step-by-Step Walkthrough</b></summary>

<br/>

### 1 · Screen — Select screen source
<img src="docs/screenshots/03-screens.png" alt="Screen selection" width="100%"/>

### 2 · Elements — Detect elements & snap bounding boxes
<img src="docs/screenshots/07-elements.png" alt="Element detection" width="100%"/>
A vision LLM proposes bounding candidates; OpenCV snaps each edge to the nearest strong gradient. Hand-editable box boundaries allow interactive fine-tuning.

### 3 · Build — Extract components into the asset library
<img src="docs/screenshots/08-build.png" alt="Build step" width="100%"/>
Interactive approval surfaces existing matching assets in the domain to prevent duplicates.

### 4 · Text — Handle text captions
Text is separated into data representations (`MockupLabel`) rather than hard-baking pixels into sprites, enabling clean localizable rendering in Unity.

### 5 · Polish — Cosmetic refinement
Runs Real-ESRGAN upscaling and edge anti-aliasing passes.

### 6 · Result — Compare score & export
<img src="docs/screenshots/10-result.png" alt="Result comparison" width="100%"/>
Provides a live visual slider comparing the original mockup against the rebuilt screen, along with an automated fidelity score.

</details>

---

## 🔬 Extraction Engine

The core pipeline uses box-prompted **HQ-SAM**, followed by closed-form **alpha matting** and dynamic bounding box growth.

<details>
<summary><b>📐 Algorithmic Details & Implementation Notes</b></summary>

<br/>

### Segmentation & Matting Pipeline

```mermaid
flowchart TB
    A["📱 Source screenshot<br/><i>supersampled & cached</i>"] --> B["1️⃣ Segment"]
    B --> B1["HQ-SAM, box-prompted"]
    B --> B2["GrabCut fallback<br/><i>offline, classical</i>"]
    B1 & B2 --> C["2️⃣ Matte"]
    C --> C1["Trimap from mask"]
    C1 --> C2["Closed-form alpha matting"]
    C2 --> C3["Germer multi-level<br/>foreground estimation"]
    C3 --> D["3️⃣ Grow (if clipped)"]
    D --> D1["Anchored to original detect_rect"]
    D1 --> E["✅ Transparent PNG<br/><i>+ fidelity metrics</i>"]

    style A fill:#2a3550,stroke:#6c8cff,color:#eef0f2
    style B fill:#1f3d2b,stroke:#3fb950,color:#eef0f2
    style C fill:#1f3d2b,stroke:#3fb950,color:#eef0f2
    style D fill:#3d2b1f,stroke:#f5b820,color:#eef0f2
    style E fill:#2b2233,stroke:#8a4fd6,color:#eef0f2
```

### Key Technical Insights

- **HQ-SAM vs GrabCut**: HQ-SAM prevents background bleed on thin diagonal objects. Classical GrabCut seed boxes can accidentally capture background pixels inside narrow bounding boxes, leading to color corruption.
- **Exterior Background Sampling**: Background seeds are sampled from *outside* the detected bounding rectangle to prevent trimming natural element borders and bevels.
- **Anchored Grow Logic**: Box expansion anchors strictly to the original `detect_rect`, preventing iterative growth loops from swallowing neighboring UI elements.
- **De-occlusion Inpainting**: `processing/inpaint.py` masks foreground occluders and leverages LaMa inpainting to reconstruct clean background frames.

</details>

---

## 🗃️ Data Model & Storage

<details>
<summary><b>🔍 Database Schema (ER Diagram) & Disk Layout</b></summary>

<br/>

### Database ER Diagram

```mermaid
erDiagram
    PROJECT ||--o{ ATLAS : "has domains"
    PROJECT ||--o{ ASSET : owns
    PROJECT ||--o{ MOCKUP : "has screens"
    ATLAS ||--o{ ATLAS : "parent of"
    ATLAS ||--o{ ASSET : groups
    ASSET ||--o{ ASSET_VERSION : "keeps history"
    MOCKUP ||--o{ MOCKUP_REGION : "boxes"
    MOCKUP ||--o{ MOCKUP_LABEL : "captions"
    MOCKUP_REGION }o--|| ASSET : "binds to"

    PROJECT {
        string name
        text style_description
        json palette
        text unity_path
        int ppu
        string filter_mode "point / bilinear"
        string wrap_mode "clamp / repeat"
        bool power_of_two
    }
    ATLAS {
        string name
        int parent_id "self-referential tree"
        text export_path "override under Assets/Sprites/"
    }
    ASSET {
        string name
        string type "ui_element / icon / sprite / tile / sprite_sheet"
        text prompt
        string aspect_ratio "W:H"
        string resolution "WxH"
        json nine_slice "l,t,r,b px"
        string source "extract / generate / manual"
        int selected_version_id
    }
    ASSET_VERSION {
        string provider
        string model
        text composed_prompt
        text raw_path
        text processed_path
        json fidelity "delta_e, ssim, alpha_iou, coverage, score"
    }
    MOCKUP_REGION {
        float x_y_w_h "percentages of the image"
        json detect_rect "the rect as first proposed"
        string template "shared-background group"
        bool mirror
        bool force_rebuild
    }
    MOCKUP_LABEL {
        text text
        float x_y_w_h
        string color
        string text_mode "keep / erase / extract"
    }
```

### Storage Directory Structure

Managed by `server/app/layout.py`:

```
storage/
├── app.db                                              # SQLite main database file
└── projects/<pid>/
    ├── domains/<Domain>/<SubDomain>/<AssetName>/       # Asset source and versions
    ├── domains/_unassigned/<AssetName>/                # Unassigned assets
    ├── mockups/<mockup_id>/                            # Original screenshots & crops
    ├── refs/                                           # Project style reference images
    ├── previews/, _work/                               # Temporary working files
    └── runs/                                           # Execution progress logs
```

- **File Management**: Paths are generated via `storage.new_asset_path()`. Asset updates trigger directory reconciliations (`layout.reconcile`).
- **Maintenance CLI**: `python -m tools.migrate_storage --apply --prune` cleans unreferenced storage artifacts.

</details>

---

## 🎮 Unity Integration

Exports raw PNG assets, `.import.json` sidecars, and automated Unity Editor C# scripts to streamline asset ingestion into Unity projects.

```mermaid
flowchart LR
    A["Asset library<br/><i>grouped by domain</i>"] --> B["PNG + .import.json"]
    B --> C["Assets/Sprites/&lt;path&gt;/&lt;Domain&gt;/"]
    C --> D["AssetPipelineImporter.cs<br/><i>applies PPU, filter, wrap, 9-slice</i>"]
    D --> E["SpriteAtlasBuilder.cs<br/><i>one .spriteatlas per domain</i>"]
    C --> F["ScreenLayoutBuilder.cs<br/><i>rebuilds the screen as a prefab</i>"]

    style A fill:#2a3550,stroke:#6c8cff,color:#eef0f2
    style E fill:#2b2233,stroke:#8a4fd6,color:#eef0f2
    style F fill:#2b2233,stroke:#8a4fd6,color:#eef0f2
```

<details>
<summary><b>🖼️ Asset Detail UI & Unity Importer Screenshots</b></summary>

<br/>

<img src="docs/screenshots/11-asset-detail.png" alt="Asset detail" width="100%"/>

Inspect composed prompts, version history, resolution parameters, PPU, and 9-slice borders directly in the editor interface.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/06-settings.png" alt="Project settings"/></td>
<td width="50%"><img src="docs/screenshots/05-providers.png" alt="Providers"/></td>
</tr>
<tr>
<td><b>Project Settings</b> — Global default settings for Unity import, PPU, and texture filtering.</td>
<td><b>Provider Controls</b> — Hard server-side toggles for API expenditure control.</td>
</tr>
</table>

</details>

---

## 🧪 Testing & Quality Gates

The test suite covers the image-processing core, validating algorithms without requiring live ML models or cloud API keys.

```bash
# Run test suite and measure coverage
cd server
pytest tests --cov --cov-report=term
```

<details>
<summary><b>📈 Coverage Breakdown & CI Patch Gate</b></summary>

<br/>

### Module Coverage Matrix

| Module | Coverage | Status |
|---|---:|---|
| `schemas.py` | 100% | ██████████ |
| `processing/fidelity.py` | 97% | █████████▊ |
| `processing/subdivide.py` | 95% | █████████▌ |
| `models.py` · `processing/reference.py` | 94% | █████████▍ |
| `processing/extract.py` | 79% | ███████▉ |
| `prompting.py` | 78% | ███████▊ |
| `processing/region_detector.py` | 77% | ███████▋ |
| `processing/nine_slice.py` | 76% | ███████▌ |
| `processing/composite.py` | 62% | ██████▏ |
| `processing/inpaint.py` | 55% | █████▌ |
| `routers/*` · `providers/*` | 0–46% | ▏ |
| **Total Core Engine** | **32%** | ███▏ |

### CI Workflow Gate

```mermaid
flowchart LR
    PR["📥 Pull request"] --> J1["server tests + coverage"]
    PR --> J2["client typecheck + lint + build"]
    J1 --> DC["🚦 Patch coverage gate<br/><i>diff-cover vs. origin/main</i><br/><b>fails under 80%</b>"]
    J1 -.->|"reporting"| CC["📊 Codecov"]
    DC --> G{"Required checks green?"}
    J2 --> G
    G -->|"yes"| M["✅ Merge allowed"]
    G -->|"no"| B["🚫 Merge blocked"]

    style PR fill:#2a3550,stroke:#6c8cff,color:#eef0f2
    style DC fill:#3d2b1f,stroke:#f5b820,color:#eef0f2
    style M fill:#1f3d2b,stroke:#3fb950,color:#eef0f2
    style B fill:#3d2222,stroke:#d63a3a,color:#eef0f2
```

Every PR requires that newly added or modified lines in `server/app` maintain at least **80% patch test coverage**, enforced locally via `diff-cover`:

```bash
pytest tests --cov --cov-report=xml && diff-cover coverage.xml --compare-branch=origin/main --fail-under=80
```

</details>

---

## 🤝 Contributing

1. Create a feature branch: `git checkout -b feature/my-improvement`
2. Run local tests: `cd server && pytest tests`
3. Check code formatting: `npm --prefix client run lint`
4. Submit a pull request.

<details>
<summary><b>🔒 Branch Protection & Merge Policies</b></summary>

<br/>

| Gate | Policy |
|---|---|
| `server tests + coverage` | All unit tests pass & patch coverage ≥ 80% |
| `client typecheck + lint + build` | `oxlint`, `tsc -b`, and `vite build` must pass cleanly |
| `main` Branch Safety | Direct force-pushes and branch deletions are disabled |

</details>

---

<div align="center">

**Built for turning game-UI mockups into shippable Unity sprites — without redrawing them.**

</div>
