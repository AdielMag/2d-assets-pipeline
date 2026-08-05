# Start both server and client, output URLs
$projectRoot = Split-Path -Parent $PSCommandPath
$serverPath = Join-Path $projectRoot "server"
$clientPath = Join-Path $projectRoot "client"
$pythonExe = Join-Path $serverPath ".venv\Scripts\python.exe"

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
    Stop-Process -InputObject $serverJob -ErrorAction SilentlyContinue -Force
    Stop-Process -InputObject $clientJob -ErrorAction SilentlyContinue -Force
    Write-Host "✅ Servers stopped" -ForegroundColor Green
}
