#!/bin/bash
# ── nomad card tools — Linux/macOS launcher ──────────────────────────────────
# Run ./start-linux.sh to open the tools menu.
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 isn't installed or isn't on PATH."
    echo "Install it with your package manager (e.g. sudo apt install python3) and retry."
    exit 1
fi

exec python3 nomad-tools.py "$@"
