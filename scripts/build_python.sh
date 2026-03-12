#!/bin/bash
set -e

cd "$(dirname "$0")/.."

source .venv/bin/activate

# Clean previous build
rm -rf src-tauri/bin

# Build sidecar — COLLECT name='bin' so output goes to src-tauri/bin/
pyinstaller spec/backend_linux.spec --distpath src-tauri --clean --noconfirm --log-level ERROR

echo "Python sidecar build complete."
