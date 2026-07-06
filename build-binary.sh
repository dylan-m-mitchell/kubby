#!/usr/bin/env bash
# build-binary.sh — build a single-file kubby binary via PyInstaller.
#
# Prerequisite: pip install pyinstaller
#   (or: uv pip install pyinstaller ; uv run pyinstaller ...)
#
# Usage:  ./build-binary.sh
# Output: dist/kubby
#
# Re-runs are incremental; pass --clean to wipe build/ and dist/ first
# (rarely needed — pyinstaller is good at picking up source edits).
set -euo pipefail
cd "$(dirname "$0")"

# Sanity-check that pyinstaller is on PATH. We deliberately don't pin a
# version in pyproject.toml because the build is out-of-band (a developer
# runs this script, not the user). The README has the install line.
if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "error: pyinstaller not found on PATH" >&2
    echo "       install it with:  pip install pyinstaller" >&2
    exit 127
fi

pyinstaller --noconfirm kubby.spec

echo
echo "Built: dist/kubby"
ls -lh dist/kubby
file dist/kubby 2>/dev/null || true
