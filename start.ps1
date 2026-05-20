# Launch Screen-workflow in live mode. Run after .\setup.ps1.
#   .\start.ps1                  -> 15-minute capture
#   .\start.ps1 -Seconds 60      -> 1-minute test run
#   .\start.ps1 -Reset           -> wipe previous local_data first

param(
    [double]$Seconds = 900,
    [switch]$Reset
)

$ErrorActionPreference = "Stop"

if ($Reset) {
    Write-Host "[start] wiping previous local_data and viz_output..." -ForegroundColor Yellow
    if (Test-Path "local_data") { Remove-Item -Recurse -Force local_data }
    if (Test-Path "viz_output") { Remove-Item -Recurse -Force viz_output }
}

# Always wipe stale Python bytecode so file edits take effect on next import.
Get-ChildItem -Path . -Recurse -Filter __pycache__ -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

. .\.venv\Scripts\Activate.ps1
screen-workflow-live --root .\local_data --seconds $Seconds --verbose
