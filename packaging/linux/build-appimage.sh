#!/usr/bin/env bash
# Build the AppImage: Stratum-${VERSION}-x86_64.AppImage (docs/distribution.md).
#
# Runs on ubuntu-latest in CI on a version tag. The script
#
#   1. builds the wheel (python -m build --wheel),
#   2. assembles an AppDir (payload under usr/lib/stratum-memory, the CLI
#      launcher under usr/bin/stratum, AppRun + .desktop + icon at the root),
#   3. downloads appimagetool (pinned release) and packs the AppDir.
#
# VERSION is *set by the CI* (from src/stratum/__init__.py, the version source
# named in the contract); there is deliberately no fallback, so a manual run
# without VERSION stops instead of quietly building 0.1.0. appimagetool is
# fetched from its official GitHub release because it cannot be installed via a
# package manager; the download is HTTPS and the checksum-free release binary is
# the upstream-published artefact.
#
# Usage:  VERSION=0.1.0 bash packaging/linux/build-appimage.sh
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

VERSION="${VERSION:?set VERSION (see release.yml)}"
DIST_DIR="${DIST_DIR:-$REPO_ROOT/dist}"
ARCH="${ARCH:-x86_64}"
APPIMAGETOOL_URL="${APPIMAGETOOL_URL:-https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage}"

APPDIR="$REPO_ROOT/build/Stratum.AppDir"
OUTPUT="$DIST_DIR/Stratum-${VERSION}-${ARCH}.AppImage"

mkdir -p "$DIST_DIR" "$APPDIR/usr/bin" "$APPDIR/usr/lib/stratum-memory" \
    "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# 1. The wheel.
echo "==> building the wheel (stratum-memory ${VERSION})"
python -m build --wheel

# 2. Assemble the AppDir.
echo "==> assembling the AppDir"
# Install the freshly built wheel (and its dependencies) into the payload.
python -m pip install --no-cache-dir --upgrade \
    --target "$APPDIR/usr/lib/stratum-memory" \
    dist/stratum-"${VERSION}"-py3-none-any.whl

install -m 0755 packaging/linux/stratum-launcher.sh "$APPDIR/usr/bin/stratum"
install -m 0755 packaging/linux/AppRun "$APPDIR/AppRun"
install -m 0644 packaging/linux/stratum.desktop "$APPDIR/stratum.desktop"
install -m 0644 packaging/linux/stratum.desktop "$APPDIR/usr/share/applications/stratum.desktop"
if [ -f assets/logo/stratum-icon-256.png ]; then
    install -m 0644 assets/logo/stratum-icon-256.png "$APPDIR/stratum.png"
    install -m 0644 assets/logo/stratum-icon-256.png \
        "$APPDIR/usr/share/icons/hicolor/256x256/apps/stratum.png"
fi

# 3. Fetch appimagetool and pack.
TOOL="$REPO_ROOT/build/appimagetool-${ARCH}.AppImage"
echo "==> downloading appimagetool"
curl -fsSL -o "$TOOL" "$APPIMAGETOOL_URL"
chmod +x "$TOOL"

echo "==> packing Stratum-${VERSION}-${ARCH}.AppImage"
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT"

echo "==> artefact: $OUTPUT"
ls -l "$OUTPUT"
