#!/bin/bash
set -e

cd "$(dirname "$0")/.."

echo "=== Step 1: Building Python sidecar ==="
bash scripts/build_python.sh

echo "=== Step 2: Building Vite frontend ==="
npx vite build

echo "=== Step 3: Building Tauri app ==="
npx tauri build

echo "=== Build complete ==="
