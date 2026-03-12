#!/bin/bash
set -e

cd "$(dirname "$0")/.."

source .venv/bin/activate

# Clean all previous build artifacts and PyInstaller caches
rm -rf src-tauri/bin build dist
find src-python -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Build sidecar — COLLECT name='bin' so output goes to src-tauri/bin/
pyinstaller spec/backend_linux.spec --distpath src-tauri --clean --noconfirm --log-level ERROR

echo "Python sidecar build complete."
