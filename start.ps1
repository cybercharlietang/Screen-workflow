# Launch Screen-workflow in live mode. Run after .\setup.ps1.
#   .\start.ps1                  -> 15-minute capture
#   .\start.ps1 -Seconds 60      -> 1-minute test run
#   .\start.ps1 -Reset           -> wipe previous local_data first
#   .\start.ps1 -ApiKey sk-...   -> use this key for this run only
#
# API key resolution (first hit wins):
#   1. -ApiKey argument
#   2. existing $env:ANTHROPIC_API_KEY in this shell
#   3. a local, gitignored .env file containing: ANTHROPIC_API_KEY=sk-ant-...
# The key is set for THIS process only — never a global/User env var.

param(
    [double]$Seconds = 900,
    [switch]$Reset,
    [string]$ApiKey = ""
)

$ErrorActionPreference = "Stop"

if ($ApiKey) {
    $env:ANTHROPIC_API_KEY = $ApiKey.Trim()
}
elseif (-not $env:ANTHROPIC_API_KEY -and (Test-Path ".\.env")) {
    foreach ($line in Get-Content ".\.env") {
        if ($line -match '^\s*ANTHROPIC_API_KEY\s*=\s*(.+?)\s*$') {
            $env:ANTHROPIC_API_KEY = $matches[1].Trim('"').Trim("'")
        }
    }
}

if ($env:ANTHROPIC_API_KEY) {
    $tail = $env:ANTHROPIC_API_KEY.Substring([Math]::Max(0, $env:ANTHROPIC_API_KEY.Length - 4))
    Write-Host "[start] ANTHROPIC_API_KEY loaded (ending ...$tail)" -ForegroundColor Green
}
else {
    Write-Host "[start] no ANTHROPIC_API_KEY found - labeler will be disabled" -ForegroundColor Yellow
}

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
