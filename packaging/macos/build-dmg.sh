#!/bin/sh
# ---------------------------------------------------------------------------
# Stratum-Memory - macOS installer: build the .dmg.
#
# Run by the CI on a `macos-latest` runner, after build-pkg.sh has produced the
# .pkg. The runner exports VERSION (read from `src/stratum/__init__.py`,
# `__version__`); this script NEVER hardcodes a version.
#
# Result (naming contract, see docs/distribution.md):
#   Stratum-<version>-x86_64.dmg
#
# The mounted volume is named `Stratum-Memory` and contains:
#   Stratum-<version>-x86_64.pkg   double-click to run the installer
#   Applications                   symlink to /Applications (drag target)
#
# Usage:
#   VERSION=0.1.0 sh packaging/macos/build-dmg.sh [PKG_PATH] [OUTPUT_DIR]
# ---------------------------------------------------------------------------

set -eu

log() { printf '%s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# ---------------------------------------------------------------- inputs ----
# The CI sets VERSION; it must never be hardcoded here.
VERSION="${VERSION:?the CI must export VERSION (from src/stratum/__init__.py)}"
OUTPUT_DIR="${2:-$REPO_ROOT/dist}"
VOLUME_NAME="Stratum-Memory"

case "$VERSION" in
    '' | *[!0-9A-Za-z._-]*) die "VERSION must be a plain version string (got '$VERSION')" ;;
esac

# The two contract file names (docs/distribution.md) are derived here.
PKG_NAME="Stratum-${VERSION}-x86_64.pkg"
DMG_NAME="Stratum-${VERSION}-x86_64.dmg"
PKG_PATH="${1:-$OUTPUT_DIR/$PKG_NAME}"

# ------------------------------------------------------------- tool check ----
command -v hdiutil >/dev/null 2>&1 || die "hdiutil not found (run this on macOS)"
[ -f "$PKG_PATH" ] || die "package not found: $PKG_PATH (run build-pkg.sh first)"

log "Stratum-Memory .dmg builder"
log "  version: $VERSION"
log "  volume:  $VOLUME_NAME"
log "  pkg:     $PKG_PATH"
log "  output:  $OUTPUT_DIR/$DMG_NAME"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

# ---------------------------------------------------------------- staging ----
STAGE="$WORK/stage"
mkdir -p "$STAGE"
cp "$PKG_PATH" "$STAGE/$PKG_NAME"
ln -s /Applications "$STAGE/Applications"

mkdir -p "$OUTPUT_DIR"

# ------------------------------------------- create + convert (compressed) ----
TEMP_DMG="$WORK/stratum-temp.dmg"
hdiutil create \
    -srcfolder "$STAGE" \
    -volname "$VOLUME_NAME" \
    -fs HFS+ \
    -format UDRW \
    -ov \
    "$TEMP_DMG"

hdiutil convert "$TEMP_DMG" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$OUTPUT_DIR/$DMG_NAME"

log "built: $OUTPUT_DIR/$DMG_NAME"
