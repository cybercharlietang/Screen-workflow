# Screen-workflow one-shot Windows setup.
# Run from inside the Screen-workflow repo directory:
#   .\setup.ps1
#
# After it finishes you can launch the live daemon with:
#   .\start.ps1
#
# PowerShell 5.x compatible.

$ErrorActionPreference = "Stop"

Write-Host "[setup] checking Python..." -ForegroundColor Cyan
python --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python is not on PATH. Install Python 3.11+ from https://python.org and check 'Add to PATH'." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".venv")) {
    Write-Host "[setup] creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "[setup] activating virtual environment..." -ForegroundColor Cyan
. .\.venv\Scripts\Activate.ps1

Write-Host "[setup] installing package..." -ForegroundColor Cyan
pip install --upgrade pip
pip install -e .
pip install pywin32

Write-Host ""
Write-Host "[setup] done. Run    .\start.ps1    to launch the live daemon." -ForegroundColor Green
