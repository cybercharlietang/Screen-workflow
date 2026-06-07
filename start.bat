@echo off
REM Double-click this to launch Screen-workflow live capture.
REM   - Reads your key from the gitignored .env file (via start.ps1).
REM   - Runs for the duration below, or until you press Ctrl+C / close the window.
REM Change SECONDS to set the run length:  7200 = 2 hours, 21600 = 6 hours.

setlocal
cd /d "%~dp0"
set SECONDS=7200

powershell -ExecutionPolicy Bypass -NoExit -File ".\start.ps1" -Seconds %SECONDS%
