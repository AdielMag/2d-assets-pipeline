# Starting the Dev Servers

Start both the FastAPI server and Vite client with one command:

## Quick Start

### PowerShell
```powershell
.\start-dev.ps1
```

### Command Prompt / Windows Terminal
```cmd
start-dev.cmd
```

## What It Does

- **Server**: FastAPI + Uvicorn on `http://localhost:8787`
- **Client**: Vite + React on `http://localhost:5173`

Both servers run in parallel. Output shows:
```
✅ Servers started!

📱 Client:  http://localhost:5173
🔌 API:     http://localhost:8787

Press Ctrl+C to stop all servers
```

## Manual Start (Alternative)

If you prefer running them separately in different terminals:

### Terminal 1 - Server
```powershell
cd server
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --port 8787
```

### Terminal 2 - Client
```powershell
cd client
npm run dev
```

Then open `http://localhost:5173` in your browser.
