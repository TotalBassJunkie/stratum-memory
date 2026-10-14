#!/bin/sh
# Stratum-Memory CLI launcher for the macOS .pkg.
#
# Installed by packaging/macos/build-pkg.sh as
#     /usr/local/lib/stratum/bin/stratum   (canonical launcher)
# and linked by packaging/macos/scripts/postinstall as
#     /usr/local/bin/stratum               (on PATH)
#
# It is a thin, POSIX-sh wrapper around the entry point declared in
# pyproject.toml:
#
#     stratum = "stratum.cli:main"
#
# The CLI and its runtime dependencies live in /usr/local/lib/stratum, which is
# put on PYTHONPATH. Override the interpreter with STRATUM_PYTHON when the
# system `python3` is not the one carrying the package.
set -eu

STRATUM_HOME="${STRATUM_HOME:-/usr/local/lib/stratum}"
PYTHON="${STRATUM_PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "stratum: '$PYTHON' not found on PATH; Python >= 3.12 is required." >&2
    echo "Install it with:  brew install python@3.12" >&2
    exit 127
fi

PYTHONPATH="$STRATUM_HOME${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PYTHON" -m stratum.cli "$@"
