#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d ".venv" ]; then
    echo ".venv not found. Run ./install.sh first."
    exit 1
fi

source .venv/bin/activate
python src/gui_app.py "$@"
