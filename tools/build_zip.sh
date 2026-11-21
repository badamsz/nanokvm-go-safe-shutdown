#!/usr/bin/env bash
# Build dist/safe-shutdown-<version>.zip for Settings > Apps > Installed > Upload ZIP.
# The archive holds exactly one top-level folder, as NanoKVM Go requires.
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(python3 -c "import json;print(json.load(open('safe-shutdown/app.json'))['version'])")
mkdir -p dist
out="dist/safe-shutdown-${version}.zip"
rm -f "$out"
python3 -m py_compile safe-shutdown/*.py
zip -qr "$out" safe-shutdown -x '*/__pycache__/*' '*.pyc' '*/.DS_Store'
echo "$out"
