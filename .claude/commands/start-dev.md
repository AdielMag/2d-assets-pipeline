---
model: claude-haiku-4-5-20251001
---

Start the 2D Assets Pipeline dev servers and report the URLs.

Use the browser-preview tool to launch both configurations defined in `.claude/launch.json`:

1. Start the `server` config (FastAPI/Uvicorn) — runs on port 8787.
2. Start the `client` config (Vite) — runs on port 5173.
3. Report both URLs back to the user:
   - Client: http://localhost:5173
   - API: http://localhost:8787

If either server is already running, just report its existing URL instead of restarting it.
