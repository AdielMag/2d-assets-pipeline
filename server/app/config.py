from pathlib import Path

from dotenv import load_dotenv
import os
import shutil

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
STORAGE_DIR = REPO_DIR / "storage"
UNITY_TEMPLATE_DIR = REPO_DIR / "unity"
DB_PATH = STORAGE_DIR / "app.db"

load_dotenv(SERVER_DIR / ".env")

# When set, the Gemini provider uses the Generative Language REST API directly instead
# of the Gemini CLI + Nano Banana extension.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# Antigravity image generation runs through Google's Antigravity CLI agent, which
# uses the user's Google AI Pro/Ultra subscription (no per-image API billing).
# The binary name and any leading args are overridable in case the installed CLI
# differs from the default. ANTIGRAVITY_ARGS is space-split (simple cases only).
ANTIGRAVITY_BIN = os.getenv(
    "ANTIGRAVITY_BIN",
    "antigravity" if shutil.which("antigravity") else ("agy" if shutil.which("agy") else "antigravity")
)
ANTIGRAVITY_ARGS = os.getenv("ANTIGRAVITY_ARGS", "")

# Higgsfield image generation runs through the official `higgsfield` CLI
# (npm i -g @higgsfield/cli), authenticated via `higgsfield auth login` against the
# user's own Higgsfield plan (Starter/Plus/Ultra) — same shape as Antigravity: no
# API key, uses the account's own subscription/credits.
HIGGSFIELD_BIN = os.getenv(
    "HIGGSFIELD_BIN",
    "higgsfield" if shutil.which("higgsfield") else ("hf" if shutil.which("hf") else "higgsfield")
)
HIGGSFIELD_MODEL = os.getenv("HIGGSFIELD_MODEL", "nano_banana_flash")

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

