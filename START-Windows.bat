@echo off
REM nomad card tools - Windows launcher
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

REM The first run sets up .venv beside the tools (the libraries in requirements.txt,
REM needs internet once); after that it starts straight away, and sets up again only
REM when requirements.txt changes.
set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "SETUP="
if not exist "%VPY%" set "SETUP=1"
if not defined SETUP ( fc /b requirements.txt "%VENV%\.requirements" >nul 2>&1 || set "SETUP=1" )
if defined SETUP (
    echo Setting up the libraries the tools need. This only happens once, and it needs internet...
    if not exist "%VPY%" %PY% -m venv --system-site-packages "%VENV%"
    "%VPY%" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo Couldn't install the libraries. Check the internet connection and run this again.
        echo.
        pause
        exit /b 1
    )
    copy /y requirements.txt "%VENV%\.requirements" >nul
)

"%VPY%" nomad-tools.py
echo.
pause
endlocal
