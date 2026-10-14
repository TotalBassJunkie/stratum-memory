#!/bin/sh
# ---------------------------------------------------------------------------
# Stratum-Memory - macOS installer: build the .pkg.
#
# Run by the CI on a `macos-latest` runner. The runner exports VERSION (read
# from `src/stratum/__init__.py`, `__version__`); this script NEVER hardcodes a
# version and fails fast when VERSION is unset.
#
# Result (naming contract, see docs/distribution.md):
#   Stratum-<version>-x86_64.pkg
#
# Installed payload:
#   /usr/local/lib/stratum/          the `stratum` CLI + its dependencies
#   /usr/local/lib/stratum/bin/      the canonical launcher
#   /usr/local/bin/stratum           launcher (scripts/postinstall normalises it
#                                    into a symlink to the canonical one)
#
# The payload is built with `pkgbuild` (component package) and wrapped into the
# final product by `productbuild` using `distribution.xml`. No secrets, no
# network calls of our own beyond what `pip` needs to resolve dependencies.
#
# Usage:
#   VERSION=0.1.0 sh packaging/macos/build-pkg.sh [OUTPUT_DIR]
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
IDENTIFIER="${IDENTIFIER:-dev.stratum.stratum-memory}"
OUTPUT_DIR="${1:-$REPO_ROOT/dist}"
PYTHON="${PYTHON:-python3}"

case "$VERSION" in
    '' | *[!0-9A-Za-z._-]*) die "VERSION must be a plain version string (got '$VERSION')" ;;
esac

# The two contract file names (docs/distribution.md) are derived here.
PKG_NAME="Stratum-${VERSION}-x86_64.pkg"
COMPONENT_PKG="$IDENTIFIER.pkg"

# ------------------------------------------------------------- tool check ----
command -v pkgbuild >/dev/null 2>&1 || die "pkgbuild not found (run this on macOS)"
command -v productbuild >/dev/null 2>&1 || die "productbuild not found (run this on macOS)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM
PAYLOAD="$WORK/payload"

# ---------------------------------------------------------------- payload ----
log "Stratum-Memory .pkg builder"
log "  version:    $VERSION"
log "  identifier: $IDENTIFIER"
log "  output:     $OUTPUT_DIR/$PKG_NAME"

mkdir -p "$PAYLOAD/usr/local/lib/stratum" "$PAYLOAD/usr/local/bin"

# Prefer a self-contained install (CLI + runtime dependencies) via pip; fall
# back to shipping the source tree when pip cannot reach an index.
if "$PYTHON" -m pip install --quiet --upgrade --target "$PAYLOAD/usr/local/lib/stratum" "$REPO_ROOT"; then
    log "payload: installed the CLI and its dependencies with 'pip install --target'"
else
    log "payload: 'pip install --target' unavailable; copying the source tree instead"
    mkdir -p "$PAYLOAD/usr/local/lib/stratum/stratum"
    cp -R "$REPO_ROOT/src/stratum/." "$PAYLOAD/usr/local/lib/stratum/stratum/"
fi

# The canonical launcher (packaging/macos/stratum-launcher.sh) plus the
# /usr/local/bin copy. `scripts/postinstall` later turns the /usr/local/bin
# entry into a symlink to the canonical one.
LAUNCHER="$HERE/stratum-launcher.sh"
[ -f "$LAUNCHER" ] || die "launcher not found: $LAUNCHER"

mkdir -p "$PAYLOAD/usr/local/lib/stratum/bin"
install -m 0755 "$LAUNCHER" "$PAYLOAD/usr/local/lib/stratum/bin/stratum"
install -m 0755 "$LAUNCHER" "$PAYLOAD/usr/local/bin/stratum"

# ----------------------------------------------------- component + product ----
pkgbuild \
    --root "$PAYLOAD" \
    --identifier "$IDENTIFIER" \
    --version "$VERSION" \
    --scripts "$HERE/scripts" \
    --install-location / \
    "$WORK/$COMPONENT_PKG"

# distribution.xml keeps placeholders so the version/identifier are never
# baked into a tracked file; substitute them into the working copy.
sed -e "s|__VERSION__|$VERSION|g" -e "s|__IDENTIFIER__|$IDENTIFIER|g" \
    "$HERE/distribution.xml" > "$WORK/distribution.xml"

mkdir -p "$OUTPUT_DIR"
productbuild \
    --distribution "$WORK/distribution.xml" \
    --resources "$HERE/resources" \
    --package-path "$WORK" \
    "$OUTPUT_DIR/$PKG_NAME"

log "built: $OUTPUT_DIR/$PKG_NAME"
