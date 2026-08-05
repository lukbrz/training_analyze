#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v python >/dev/null 2>&1; then
    echo "python3.12 is required (garminconnect needs Python >=3.12) but was not found." >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv ..."
    python -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Done. Run ./run.sh to start the app."
