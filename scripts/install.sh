#!/bin/bash
set -e

cd "$(dirname "$0")/.."

# Remove existing venv if present
if [ -d .venv ]; then
    rm -rf .venv
fi

# Create venv with python3.12
python3.12 -m venv .venv

# Install packages
source .venv/bin/activate
python -m pip install --upgrade pip
pip install --no-cache-dir -r requirements_linux.txt

echo "Python venv setup complete."
