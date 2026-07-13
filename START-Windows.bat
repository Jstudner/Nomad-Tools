@echo off
REM ── nomad card tools — Windows launcher ──────────────────────────────────────
REM Double-click this file to open the tools menu.
setlocal
cd /d "%~dp0"

REM Find Python (the "py" launcher first, then python on PATH).
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )

if not defined PY (
    echo Python 3 was not found.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
)

%PY% nomad-tools.py
echo.
pause
endlocal
