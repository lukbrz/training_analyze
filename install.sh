#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# garminconnect needs Python >= 3.12; plain "python" is often missing or older (macOS, Ubuntu).
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 12))" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Python 3.12 or newer is required (garminconnect needs it) but was not found." >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment in .venv with $PYTHON ..."
    "$PYTHON" -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r src/requirements.txt

echo "Done. Run ./run.sh to start the app."
