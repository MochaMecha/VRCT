#!/bin/bash
set -e

# Thin wrapper — delegates to the full setup script.
# Use this if you only want to rebuild the Python venv
# (system deps and toolchains already installed).

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Find best Python ────────────────────────────────────────────────

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: No suitable Python 3 found."
    echo "Run 'bash scripts/setup.sh' for full setup including system deps."
    exit 1
fi

echo "Using $PYTHON_BIN ($($PYTHON_BIN --version 2>&1))"

# ── Create venv ─────────────────────────────────────────────────────

if [ -d .venv ]; then
    rm -rf .venv
fi

$PYTHON_BIN -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install --no-cache-dir -r requirements_linux.txt

echo "Python venv setup complete."
