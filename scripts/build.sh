#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

PACKAGE=false
if [ "$1" = "--package" ] || [ "$1" = "-p" ]; then
    PACKAGE=true
fi

# ── Pre-flight checks ──────────────────────────────────────────────

if [ ! -d .venv ]; then
    echo "ERROR: Python venv not found. Run 'bash scripts/setup.sh' first."
    exit 1
fi

if [ ! -d node_modules ]; then
    echo "ERROR: node_modules not found. Run 'bash scripts/setup.sh' first."
    exit 1
fi

if ! command -v cargo &>/dev/null; then
    echo "ERROR: Rust/cargo not found. Run 'bash scripts/setup.sh' first."
    exit 1
fi

# ── Build ───────────────────────────────────────────────────────────

echo "=== Step 1: Building Python sidecar ==="
bash scripts/build_python.sh

echo "=== Step 2: Building Vite frontend ==="
npx vite build

if [ "$PACKAGE" = true ]; then
    echo "=== Step 3: Building Tauri app + packaging (deb/appimage) ==="
    npx tauri build
else
    echo "=== Step 3: Building Tauri app ==="
    npx tauri build --no-bundle
fi

echo ""
echo "=== Build complete ==="
echo "Binary: src-tauri/target/release/VRCT"
if [ "$PACKAGE" = true ]; then
    echo "Packages: src-tauri/target/release/bundle/"
else
    echo ""
    echo "To also create deb/appimage packages, run:"
    echo "  bash scripts/build.sh --package"
fi
