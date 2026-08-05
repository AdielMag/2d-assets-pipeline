# Start both server and client, output URLs
$projectRoot = Split-Path -Parent $PSCommandPath
$serverPath = Join-Path $projectRoot "server"
$clientPath = Join-Path $projectRoot "client"
$pythonExe = Join-Path $serverPath ".venv\Scripts\python.exe"

function Check-PortAndPrompt($port, $name) {
    $conns = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($conns.Count -gt 0) {
        $procId = $conns[0].OwningProcess
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            $procName = $proc.ProcessName
            Write-Host "⚠️  Port $port is already in use by process '$procName' (PID: $procId) for $name" -ForegroundColor Yellow
            $response = Read-Host "Do you want to kill this process? (y/N)"
            if ($response -match "^[yY]") {
                try {
                    Stop-Process -Id $procId -Force -ErrorAction Stop
                    Write-Host "✅ Process killed." -ForegroundColor Green
                    Start-Sleep -Seconds 1
                } catch {
                    Write-Host "❌ Failed to kill process: $_" -ForegroundColor Red
                    exit 1
                }
            } else {
                Write-Host "❌ Cannot start $name. Port $port is in use." -ForegroundColor Red
                exit 1
            }
        }
    }
}

Check-PortAndPrompt 8787 "API Server"
Check-PortAndPrompt 5173 "Client"

Write-Host "🚀 Starting 2D Assets Pipeline servers..." -ForegroundColor Cyan
Write-Host ""

# Start server
$serverJob = Start-Process -FilePath $pythonExe -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "8787" -WorkingDirectory $serverPath -WindowStyle Hidden -PassThru

# Start client
$clientJob = Start-Process -FilePath "npm.cmd" -ArgumentList "run", "dev" -WorkingDirectory $clientPath -WindowStyle Hidden -PassThru

# Wait a moment for servers to start
Start-Sleep -Seconds 3

Write-Host "✅ Servers started!" -ForegroundColor Green
Write-Host ""
Write-Host "📱 Client:  http://localhost:5173" -ForegroundColor Yellow
Write-Host "🔌 API:     http://localhost:8787" -ForegroundColor Yellow
Write-Host ""
Write-Host "Press Ctrl+C to stop all servers" -ForegroundColor Gray
Write-Host ""

# Keep script running and monitor processes
try {
    while ($true) {
        # Check if either process has exited
        if ($serverJob.HasExited -or $clientJob.HasExited) {
            if ($serverJob.HasExited) {
                Write-Host "⚠️  Server process exited" -ForegroundColor Red
            }
            if ($clientJob.HasExited) {
                Write-Host "⚠️  Client process exited" -ForegroundColor Red
            }
            break
        }
        Start-Sleep -Seconds 1
    }
}
finally {
    Write-Host ""
    Write-Host "🛑 Stopping servers..." -ForegroundColor Yellow
    if ($serverJob) { Stop-Process -Id $serverJob.Id -ErrorAction SilentlyContinue -Force }
    if ($clientJob) { Stop-Process -Id $clientJob.Id -ErrorAction SilentlyContinue -Force }
    Write-Host "✅ Servers stopped" -ForegroundColor Green
}
