#!/bin/sh
# Stratum-Memory CLI launcher.
#
# Installed as /usr/bin/stratum by the .deb / .rpm (see nfpm.yaml) and copied
# into the AppImage. It is a thin, POSIX-sh wrapper around the entry point
# declared in pyproject.toml:
#
#     stratum = "stratum.cli:main"
#
# so it needs nothing but a python3 >= 3.12 on PATH. Override the interpreter
# with STRATUM_PYTHON when the distribution's `python3` is not the one carrying
# the package.
set -eu

PYTHON="${STRATUM_PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "stratum: '$PYTHON' not found on PATH; Python >= 3.12 is required." >&2
    echo "Install it with:  apt install python3   |   dnf install python3" >&2
    exit 127
fi

exec "$PYTHON" -m stratum.cli "$@"
