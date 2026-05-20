# Wipe stale Python bytecode caches.
# Run this if a fresh `pip install -e .` source change isn't taking effect.
#   .\clean.ps1
Get-ChildItem -Path . -Recurse -Filter __pycache__ -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "[clean] __pycache__ directories removed." -ForegroundColor Green
