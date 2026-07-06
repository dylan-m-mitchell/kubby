#!/usr/bin/env bash
# build-binary.sh — build a single-file kubby binary via PyInstaller.
#
# Prerequisite: pip install pyinstaller jaraco.text
#   (or: uv pip install pyinstaller jaraco.text ; uv run pyinstaller ...)
#
# Usage:  ./build-binary.sh [--clean]
#   --clean  wipe build/ and dist/ before building
# Output: dist/kubby
#
# Re-runs are incremental by default.
set -euo pipefail
cd "$(dirname "$0")"

CLEAN=false
if [[ "${1:-}" == "--clean" ]]; then
    CLEAN=true
    shift
fi

# Sanity-check that pyinstaller is on PATH. We deliberately don't pin a
# version in pyproject.toml because the build is out-of-band (a developer
# runs this script, not the user). The README has the install line.
if ! command -v pyinstaller >/dev/null 2>&1; then
    echo "error: pyinstaller not found on PATH" >&2
    echo "       install it with:  pip install pyinstaller jaraco.text" >&2
    exit 127
fi

# jaraco.text is a dependency of setuptools ≥ 70 (used by pkg_resources at
# runtime). PyInstaller needs it available at build time to bundle it into
# the binary; without it the frozen binary will crash with PYI-5550.
if ! python -c "import jaraco.text" 2>/dev/null; then
    echo "error: jaraco.text not found in the Python environment" >&2
    echo "       install it with:  pip install jaraco.text" >&2
    exit 127
fi

if $CLEAN; then
    echo "Cleaning build/ and dist/ ..."
    rm -rf build/ dist/
fi

pyinstaller --noconfirm kubby.spec

echo
echo "Built: dist/kubby"
ls -lh dist/kubby
file dist/kubby 2>/dev/null || true
