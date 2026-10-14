#!/usr/bin/env bash
# Build the .deb and the .rpm from a single nFPM config (packaging/linux/nfpm.yaml).
#
# Runs on ubuntu-latest in CI on a version tag. Produces exactly the two assets
# named in the docs/distribution.md contract, in dist/:
#
#   dist/stratum_${VERSION}_amd64.deb
#   dist/stratum-${VERSION}-1.x86_64.rpm
#
# VERSION is *set by the CI* (from src/stratum/__init__.py, the version source
# named in the contract) and exported into this script's environment; there is
# deliberately no fallback, so a manual run without VERSION stops instead of
# quietly building 0.1.0. Nothing is downloaded from an untrusted place: nFPM is
# expected on PATH (CI installs it as a release binary), and the wheel is built
# locally.
#
# Usage:  VERSION=0.1.0 bash packaging/linux/build-deb-rpm.sh
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

VERSION="${VERSION:?set VERSION (see release.yml)}"
DIST_DIR="${DIST_DIR:-$REPO_ROOT/dist}"

if ! command -v nfpm >/dev/null 2>&1; then
    echo "build-deb-rpm.sh: 'nfpm' not found on PATH." >&2
    echo "Install it from https://github.com/goreleaser/nfpm/releases (release binary)." >&2
    exit 1
fi

mkdir -p "$DIST_DIR"

# 1. Build the wheel first, so a broken package fails the job before packaging.
echo "==> building the wheel (stratum-memory ${VERSION})"
python -m build --wheel

# 2. The .deb: Debian arch spelling is amd64.
echo "==> nFPM: .deb (amd64)"
ARCH=amd64 VERSION="$VERSION" nfpm package \
    --config packaging/linux/nfpm.yaml \
    --packager deb \
    --target "$DIST_DIR/stratum_${VERSION}_amd64.deb"

# 3. The .rpm: RPM arch spelling is x86_64, and the release field is 1.
echo "==> nFPM: .rpm (x86_64)"
ARCH=x86_64 VERSION="$VERSION" nfpm package \
    --config packaging/linux/nfpm.yaml \
    --packager rpm \
    --target "$DIST_DIR/stratum-${VERSION}-1.x86_64.rpm"

echo "==> artefacts in $DIST_DIR:"
ls -l "$DIST_DIR/stratum_${VERSION}_amd64.deb" "$DIST_DIR/stratum-${VERSION}-1.x86_64.rpm"
