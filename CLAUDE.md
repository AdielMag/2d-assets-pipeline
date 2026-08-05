# 2D Assets Pipeline — Claude Code Commands

## Commands

### Start Dev Servers
**Invoke:** `/start-dev`

Start both the FastAPI server and Vite client concurrently.

**Action:**
1. Start FastAPI server: `python -m uvicorn app.main:app --port 8787` in `server/`
2. Start Vite client: `npm run dev` in `client/`
3. Output URLs:
   - Client: http://localhost:5173
   - API: http://localhost:8787

**PowerShell:**
```powershell
.\start-dev.ps1
```

**Batch:**
```cmd
start-dev.cmd
```

---

## Project Context

- **Frontend:** React + Vite + TypeScript in `client/`
- **Backend:** FastAPI + SQLite in `server/`
- **Server port:** 8787
- **Client port:** 5173 (auto-assigns if taken)
- **Storage:** `storage/` (gitignored)
- **Venv:** `server/.venv`

## Storage Layout

Files mirror the domain tree, one folder per asset — see `server/app/layout.py`:

```
storage/projects/<pid>/domains/<Domain>/<SubDomain>/<AssetName>/   every file that asset owns
storage/projects/<pid>/domains/_unassigned/<AssetName>/            assets in no domain
storage/projects/<pid>/mockups/<mockup_id>/                        screenshot + its crops
storage/projects/<pid>/refs/                                       project style references
storage/projects/<pid>/previews/, _work/                           throwaway, safe to sweep
storage/projects/<pid>/runs/                                       run logs (progress.py)
```

- Write asset images with `storage.new_asset_path(db, asset, …)`, never `new_image_path`.
- Deleting an asset/domain/mockup/project deletes its files. Renaming or moving one moves
  them, via `layout.reconcile`, which recomputes the whole project and rewrites DB paths.
- Maintenance: `python -m tools.migrate_storage` (dry run) / `--apply --prune` to sweep
  files nothing references. `python -m tools.test_storage_layout` end-to-end checks the
  move/delete behaviour against a running server, on its own scratch project.

## Running Servers

Always ensure both servers are running for full functionality:
- The client proxies API calls to `http://localhost:8787`
- If the backend is stale, restart it (Uvicorn runs without `--reload`)
