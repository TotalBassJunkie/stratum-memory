#!/bin/sh
# ---------------------------------------------------------------------------
# Stratum-Memory - one-line installer for Linux and macOS.
#
#   curl -fsSL https://get.stratum.dev/install.sh | bash
#
# This script installs exactly one thing: the `stratum` command-line tool
# (from PyPI, via pipx and falling back to pip). It downloads nothing else and
# never pipes a remote script into a shell. The Docker stack (Postgres +
# pgvector, Caddy, worker) is NOT installed here - run `stratum install`
# afterwards to bootstrap it.
#
# Environment variables:
#   STRATUM_VERSION   version to install, e.g. "0.1.0" (default: latest)
#   STRATUM_PREFIX    "user" (default) or "system"
#   STRATUM_DRY_RUN   1 = print what would happen and change nothing
# ---------------------------------------------------------------------------

set -eu

PACKAGE="stratum-memory"
NPM_PACKAGE="stratum-memory"
MIN_PYTHON="3.12"

# --------------------------------------------------------------- output helpers

log() {
    printf '%s\n' "$*"
}

warn() {
    printf 'warning: %s\n' "$*" >&2
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# Run a command, or only describe it under STRATUM_DRY_RUN=1.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

# ------------------------------------------------------------ option parsing

DRY_RUN=0
case "${STRATUM_DRY_RUN:-0}" in
    1 | true | TRUE | yes | YES) DRY_RUN=1 ;;
    0 | false | FALSE | no | NO | "") DRY_RUN=0 ;;
    *) die "STRATUM_DRY_RUN must be 0 or 1 (got '$STRATUM_DRY_RUN')" ;;
esac

PREFIX="${STRATUM_PREFIX:-user}"
case "$PREFIX" in
    user | system) ;;
    *) die "STRATUM_PREFIX must be 'user' or 'system' (got '$PREFIX')" ;;
esac

VERSION="${STRATUM_VERSION:-latest}"
if [ "$VERSION" = "latest" ]; then
    SPEC="$PACKAGE"
else
    # Accept both "0.1.0" and the tag spelling "v0.1.0".
    case "$VERSION" in
        v*) VERSION="${VERSION#v}" ;;
    esac
    SPEC="$PACKAGE==$VERSION"
fi

# ------------------------------------------------------- OS / arch detection

OS="unknown"
ARCH="unknown"

detect_os() {
    raw_os="$(uname -s 2>/dev/null || printf 'unknown')"
    case "$raw_os" in
        Linux) OS="linux" ;;
        Darwin) OS="macos" ;;
        *) die "unsupported operating system: '$raw_os' (this installer covers Linux and macOS; on Windows use install.ps1)" ;;
    esac
}

detect_arch() {
    raw_arch="$(uname -m 2>/dev/null || printf 'unknown')"
    case "$raw_arch" in
        x86_64 | amd64) ARCH="x86_64" ;;
        arm64 | aarch64) ARCH="arm64" ;;
        *) ARCH="$raw_arch" ;;
    esac
}

# -------------------------------------------------------- Python interpreter

PYTHON=""
PYTHON_VERSION=""

find_python() {
    for candidate in python3 python; do
        if ! have "$candidate"; then
            continue
        fi
        if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1; then
            PYTHON="$candidate"
            PYTHON_VERSION="$("$candidate" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || printf 'unknown')"
            return 0
        fi
    done
    return 1
}

missing_python() {
    cat >&2 <<'EOF'
error: Stratum-Memory needs Python >= 3.12, and no suitable interpreter was found.

Install Python 3.12 (or newer) and re-run this script:

  Debian/Ubuntu   sudo apt update && sudo apt install -y python3 python3-pip python3-venv
  Fedora/RHEL     sudo dnf install -y python3 python3-pip
  openSUSE        sudo zypper install -y python3 python3-pip
  Alpine          sudo apk add python3 py3-pip
  macOS           brew install python@3.12        # https://brew.sh

Then: curl -fsSL https://get.stratum.dev/install.sh | bash
EOF
    exit 1
}

# ------------------------------------------------------------- installation

INSTALLED_WITH=""

install_cli() {
    if have pipx; then
        log "Installing $SPEC with pipx ..."
        if run pipx install "$SPEC"; then
            INSTALLED_WITH="pipx"
            return 0
        fi
        warn "pipx install failed; falling back to pip."
    else
        log "pipx not found; falling back to pip."
    fi

    if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        if [ "$PREFIX" = "user" ]; then
            log "Installing $SPEC with $PYTHON -m pip install --user ..."
            if run "$PYTHON" -m pip install --user "$SPEC"; then
                INSTALLED_WITH="pip (--user)"
                return 0
            fi
        else
            log "Installing $SPEC with $PYTHON -m pip install ..."
            if run "$PYTHON" -m pip install "$SPEC"; then
                INSTALLED_WITH="pip"
                return 0
            fi
        fi
        warn "pip install failed."
    else
        warn "$PYTHON has no working pip ('$PYTHON -m pip' is unavailable)."
    fi

    if have npm; then
        die "could not install $SPEC with pipx or pip. Try npm instead:
  npm i -g $NPM_PACKAGE
then re-run: stratum install"
    fi
    die "could not install $SPEC with pipx or pip, and npm is not available.
Install pip (or pipx) and re-run this script."
}

# Locate the freshly installed `stratum` binary, tolerating a stale PATH.
find_stratum_bin() {
    if have stratum; then
        command -v stratum
        return 0
    fi
    user_base="$("$PYTHON" -m site --user-base 2>/dev/null || printf '')"
    for candidate in \
        "$user_base/bin/stratum" \
        "$HOME/.local/bin/stratum" \
        "$HOME/.local/pipx/venvs/$PACKAGE/bin/stratum"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

verify_cli() {
    if [ "$DRY_RUN" -eq 1 ]; then
        log "dry run: skipping the 'stratum version' check."
        return 0
    fi

    STRATUM_BIN="$(find_stratum_bin || true)"
    if [ -z "$STRATUM_BIN" ]; then
        die "installed $SPEC with $INSTALLED_WITH, but the 'stratum' command is not on PATH.
Add it and re-open your shell, e.g.:
  pipx ensurepath
  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi

    if out="$("$STRATUM_BIN" version 2>&1)"; then
        log "verified: $out"
        return 0
    fi
    if out="$("$STRATUM_BIN" --version 2>&1)"; then
        log "verified: $out"
        return 0
    fi
    die "installed $SPEC with $INSTALLED_WITH, but '$STRATUM_BIN version' failed:
$out"
}

# ----------------------------------------------------------------- reporting

banner() {
    log "Stratum-Memory installer"
    log "  package:  $SPEC"
    log "  prefix:   $PREFIX"
    log "  dry run:  $DRY_RUN"
}

print_next_steps() {
    log ""
    log "Next steps:"
    log "  stratum install    # bootstrap the local stack (compose + .env, pull image)"
    log "  stratum up         # start the stack (docker compose up -d)"
    log "  stratum status     # container + health summary"
    log "  stratum doctor     # environment diagnostics (python, docker, ports)"
    if [ "$INSTALLED_WITH" = "pip (--user)" ]; then
        log ""
        log "If 'stratum' is not found in a new shell, add the user script directory:"
        log "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

# --------------------------------------------------------------------- main

main() {
    banner
    detect_os
    detect_arch
    log "  detected: $OS/$ARCH"

    if ! find_python; then
        missing_python
    fi
    log "  python:   $PYTHON ($PYTHON_VERSION)"

    install_cli
    verify_cli
    print_next_steps
}

main "$@"
