#!/bin/bash
# nomad card tools - Linux/macOS launcher
# Run ./start-linux.sh to open the tools menu. The first run sets up .venv beside the
# tools (the libraries in requirements.txt, needs internet once); after that it starts
# straight away, and sets up again only when requirements.txt changes.
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 isn't installed (or isn't on PATH)."
    echo "Install it with your package manager, for example: sudo apt install python3, then run this again."
    exit 1
fi

VENV=.venv
STAMP="$VENV/.requirements"
if [ ! -x "$VENV/bin/python" ] || ! cmp -s requirements.txt "$STAMP"; then
    echo "Setting up the libraries the tools need. This only happens once, and it needs internet..."
    if [ ! -x "$VENV/bin/python" ]; then
        if ! python3 -m venv --system-site-packages "$VENV" 2>/dev/null; then
            rm -rf "$VENV"
            echo "Python can't make a virtual environment here."
            echo "On Debian, Ubuntu or Pop!_OS: sudo apt install python3-venv, then run this again."
            exit 1
        fi
    fi
    if ! "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt; then
        echo "Couldn't install the libraries. Check the internet connection and run this again."
        echo "(The tools that don't need them still work: python3 nomad-tools.py)"
        exit 1
    fi
    cp requirements.txt "$STAMP"
fi

exec "$VENV/bin/python" nomad-tools.py "$@"
