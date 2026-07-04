#!/usr/bin/env bash
# build-deb.sh — Build the kubui .deb package (developer script).
#
# This is a DEVELOPER tool. End users should install a pre-built .deb with:
#   sudo apt install ./kubui_*.deb
#
# Usage:
#   bash build-deb.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> kubui .deb build"

# ── 1. Build dependencies (tools needed to compile/package, NOT runtime deps) ─
echo "==> 1/2. Installing build dependencies"
sudo apt-get update -qq
sudo apt-get install -y \
    build-essential \
    debhelper \
    dh-python \
    python3-all \
    python3-venv \
    python3-pip \
    python3-hatchling dpkg-dev

# ── 2. Build ──────────────────────────────────────────────────────────────────
echo "==> 2/2. Building the .deb"
dpkg-buildpackage -us -uc -b

DEB_FILE="$(ls -1 ../kubui_*.deb 2>/dev/null | head -1 || true)"
if [[ -z "$DEB_FILE" ]]; then
    echo "error: .deb not found after build" >&2
    exit 1
fi

echo ""
echo "==> Built: $DEB_FILE"
echo ""
echo "Install with:"
echo "  sudo apt install $DEB_FILE"
echo ""
echo "apt will automatically pull in all runtime dependencies"
echo "(python3-gi, podman, curl, etc.) and the postinst will"
echo "download kubectl, helm, and minikube."
