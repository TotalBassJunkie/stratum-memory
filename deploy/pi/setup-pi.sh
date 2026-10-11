#!/usr/bin/env bash
#
# Stratum-Memory — one-shot Pi 4 provisioner (WP-14).
#
# Run ONCE on a fresh Raspberry Pi OS install, as root. Afterwards the host is
# ready for `docker compose up`. Idempotent: re-running is safe.
#
# Steps:
#   1. sanity: running as root on Linux with an SSD present,
#   2. create the SSD directory layout (/mnt/ssd/stratum/{pgdata,kb,backups})
#      with the container UIDs as owners (pgdata=999, kb=10001) so the
#      non-root containers can write their bind mounts,
#   3. install Docker Engine + the Compose plugin if missing,
#   4. configure swap + sysctl (delegates to swap-setup.sh),
#   5. create ./secrets/* placeholders and tighten their permissions.
#
# References: MASTER-PLAN.md §10, docs/adr/0009-deployment-pi.md.
# Exit codes: 0 ok · 1 usage/precondition · 2 directory setup · 3 docker · 4 swap.
set -euo pipefail

readonly SCRIPT_NAME="${0##*/}"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

readonly EXIT_OK=0
readonly EXIT_USAGE=1
readonly EXIT_DIRS=2
readonly EXIT_DOCKER=3
readonly EXIT_SWAP=4

# Container IDs that must own the SSD bind mounts (see Dockerfile / the
# Postgres image). Mismatched ownership is the classic "permission denied on
# the bind mount" failure: the containers do NOT run as root.
readonly STRATUM_UID=10001   # Dockerfile: `useradd -m -u 10001 stratum`
readonly STRATUM_GID=10001   # ... same numeric group
readonly POSTGRES_UID=999    # pgvector/pgvector image: `postgres` user
readonly POSTGRES_GID=999

SSD_MOUNT="${STRATUM_SSD_MOUNT:-/mnt/ssd}"
DATA_ROOT="${STRATUM_DATA_ROOT:-${SSD_MOUNT}/stratum}"

log()  { printf '[%s] %s\n' "$SCRIPT_NAME" "$*" >&2; }
die()  { local code="$1"; shift; log "ERROR: $*"; exit "$code"; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "$EXIT_USAGE" "must run as root (use sudo)"
}

check_ssd() {
    if ! mountpoint -q "$SSD_MOUNT"; then
        die "$EXIT_USAGE" "SSD not mounted at $SSD_MOUNT — mount the USB-3 SSD first (never use the microSD for data)"
    fi
}

create_layout() {
    log "creating SSD layout under $DATA_ROOT"
    # Parent dirs need world-execute so the non-root container UIDs can
    # traverse them; only the leaf dirs get container-specific ownership.
    install -d -m 0755 "$DATA_ROOT" || die "$EXIT_DIRS" "cannot create $DATA_ROOT"
    # Postgres data dir: owned by the `postgres` UID inside the image (999) so the
    # DB container can write its bind mount. 0700 — nothing else needs it.
    install -d -m 0700 -o "$POSTGRES_UID" -g "$POSTGRES_GID" "$DATA_ROOT/pgdata" \
        || die "$EXIT_DIRS" "cannot create $DATA_ROOT/pgdata"
    # Git vault (markdown KB): mounted into BOTH core and worker, which run as
    # the Dockerfile's `stratum` user (UID/GID 10001). Without this ownership the
    # containers get EACCES on the bind mount.
    install -d -m 0755 -o "$STRATUM_UID" -g "$STRATUM_GID" "$DATA_ROOT/kb" \
        || die "$EXIT_DIRS" "cannot create $DATA_ROOT/kb"
    # Backups are produced/consumed by root-side host scripts.
    install -d -m 0755 "$DATA_ROOT/backups"
}

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        log "docker already installed: $(docker --version 2>/dev/null || echo present)"
        return 0
    fi
    log "installing Docker Engine + Compose plugin via get.docker.com"
    if ! command -v curl >/dev/null 2>&1; then
        die "$EXIT_DOCKER" "curl is required to install Docker"
    fi
    curl -fsSL https://get.docker.com | sh || die "$EXIT_DOCKER" "Docker install failed"
    systemctl enable --now docker || die "$EXIT_DOCKER" "could not enable docker.service"
}

configure_swap() {
    log "configuring swap / zram / sysctl"
    bash "$SCRIPT_DIR/swap-setup.sh" || die "$EXIT_SWAP" "swap-setup.sh failed"
}

create_secrets() {
    local repo_root
    repo_root="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
    local secrets_dir="${repo_root}/secrets"
    log "ensuring secrets placeholders in $secrets_dir"
    install -d -m 0700 "$secrets_dir"
    for f in pg_password app_password llm_key; do
        if [ ! -s "$secrets_dir/$f" ]; then
            ( umask 077; : > "$secrets_dir/$f" )
            log "created empty placeholder $secrets_dir/$f — FILL IT before 'docker compose up'"
        fi
        chmod 0600 "$secrets_dir/$f"
    done
}

main() {
    require_root
    check_ssd
    create_layout
    install_docker
    configure_swap
    create_secrets
    log "host setup complete — now fill secrets/ and run 'docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml up -d'"
    return "$EXIT_OK"
}

main "$@"
