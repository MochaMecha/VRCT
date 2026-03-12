#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

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

echo "=== Step 3: Building Tauri app ==="
npx tauri build

echo ""
echo "=== Build complete ==="
echo "Binary:  src-tauri/target/release/VRCT"
echo "Package: src-tauri/target/release/bundle/deb/"
