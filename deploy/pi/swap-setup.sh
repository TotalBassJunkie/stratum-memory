#!/usr/bin/env bash
#
# Stratum-Memory — host swap & VM tuning for the Raspberry Pi 4 (WP-14).
#
# Target: Raspberry Pi OS / Debian on a Pi 4 (8 GB), 24/7 operation.
# Reference: MASTER-PLAN.md §10.4, docs/adr/0009-deployment-pi.md.
#
# What this does (idempotent — safe to re-run, e.g. from stratum-host-bootstrap.service):
#   1. ensure a 4 GB swapfile on the USB-3 SSD at ${SWAPFILE} (priority 10),
#   2. ensure a 1 GB zram block device (priority 100) for *system* pages,
#   3. install /etc/sysctl.d/99-stratum.conf with swappiness / cache pressure /
#      overcommit and apply it,
#   4. never touch the microSD for swap (boot medium: wear + latency).
#
# The DB container is swap-free (memswap_limit == mem_limit, see the Compose
# override), so neither this SSD swapfile nor zram is ever handed to Postgres.
#
# Exit codes: 0 ok · 1 usage/precondition · 2 SSD/swap failure · 3 zram failure
#             · 4 sysctl failure · 5 missing dependency.
#
# Environment:
#   STRATUM_SWAPFILE       swapfile path            (default /mnt/ssd/swapfile)
#   STRATUM_SWAP_SIZE_MB   swapfile size in MiB     (default 4096 = 4 GiB)
#   STRATUM_SWAP_PRIO      SSD swapfile priority    (default 10)
#   STRATUM_ZRAM_SIZE_MB   zram size in MiB         (default 1024 = 1 GiB)
#   STRATUM_ZRAM_PRIO      zram priority            (default 100)
#   STRATUM_SSD_MOUNT      SSD mount point          (default /mnt/ssd)
#   STRATUM_SYSCTL_FILE    sysctl drop-in path      (default /etc/sysctl.d/99-stratum.conf)
#   STRATUM_DRY_RUN        1 = print actions, change nothing
#
# Deploys (next to this script): zram-config.conf -> /etc/default/zramswap.
set -euo pipefail

readonly SCRIPT_NAME="${0##*/}"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

readonly EXIT_OK=0
readonly EXIT_USAGE=1
readonly EXIT_SWAP=2
readonly EXIT_ZRAM=3
readonly EXIT_SYSCTL=4
readonly EXIT_MISSING_DEP=5

# ------------------------------------------------------------------- defaults
SWAPFILE="${STRATUM_SWAPFILE:-/mnt/ssd/swapfile}"
SWAP_SIZE_MB="${STRATUM_SWAP_SIZE_MB:-4096}"
SWAP_PRIO="${STRATUM_SWAP_PRIO:-10}"
ZRAM_SIZE_MB="${STRATUM_ZRAM_SIZE_MB:-1024}"
ZRAM_PRIO="${STRATUM_ZRAM_PRIO:-100}"
SSD_MOUNT="${STRATUM_SSD_MOUNT:-/mnt/ssd}"
SYSCTL_FILE="${STRATUM_SYSCTL_FILE:-/etc/sysctl.d/99-stratum.conf}"
ZRAM_DEFAULTS="${STRATUM_ZRAM_DEFAULTS:-/etc/default/zramswap}"
DRY_RUN="${STRATUM_DRY_RUN:-0}"

# ------------------------------------------------------------------ logging
log() {
    # log LEVEL message...
    local level="$1"; shift
    local stamp
    stamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo -)"
    printf '%s [%s] %s: %s\n' "$stamp" "$SCRIPT_NAME" "$level" "$*" >&2
}
info()  { log INFO  "$@"; }
warn()  { log WARN  "$@"; }
die()   { local code="$1"; shift; log ERROR "$@"; exit "$code"; }

# run: execute a mutating command, or only echo it in dry-run mode.
run() {
    if [ "$DRY_RUN" = "1" ]; then
        info "DRY-RUN: $*"
    else
        "$@"
    fi
}

# ------------------------------------------------------------------ prechecks
require_root() {
    if [ "$DRY_RUN" = "1" ]; then
        return 0
    fi
    if [ "$(id -u)" -ne 0 ]; then
        die "$EXIT_USAGE" "must run as root (or set STRATUM_DRY_RUN=1)"
    fi
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        die "$EXIT_MISSING_DEP" "required command not found: $1"
    fi
}

# Fail early if the SSD is not mounted where we expect the swapfile/data to be.
ensure_ssd_mounted() {
    if [ "$DRY_RUN" = "1" ]; then
        info "DRY-RUN: skip SSD mount check for $SSD_MOUNT"
        return 0
    fi
    if ! mountpoint -q "$SSD_MOUNT"; then
        die "$EXIT_USAGE" "SSD not mounted at $SSD_MOUNT (mount it first; do NOT use the microSD for swap)"
    fi
}

# ------------------------------------------------------------- 1) SSD swapfile
is_swap_active() {
    # True if $1 is currently an active swap area.
    swapon --show=NAME --noheadings 2>/dev/null | awk '{print $1}' | grep -Fxq "$1"
}

swap_priority_of() {
    # Active priority of swap area $1, or empty when it is not active.
    swapon --show=NAME,PRIO --noheadings 2>/dev/null \
        | awk -v target="$1" '$1 == target { print $2; exit }'
}

ensure_swap_priority() {
    # Idempotent priority enforcement for an *already active* swap area.
    # `swapon --priority` is a no-op on a live area (it keeps the priority it was
    # activated with), so a re-run with a changed priority would silently keep
    # the old one. Cycle it: swapoff + swapon --priority.
    local target="$1" desired="$2" current
    current="$(swap_priority_of "$target")"
    if [ -z "$current" ]; then
        return 0  # not active (any more) — the caller activates it
    fi
    if [ "$current" = "$desired" ]; then
        info "swap area already active at priority $desired: $target"
        return 0
    fi
    info "swap area $target active at priority $current != $desired; re-activating"
    run swapoff "$target" || die "$EXIT_SWAP" "swapoff failed for $target"
    run swapon --priority "$desired" "$target" \
        || die "$EXIT_SWAP" "swapon failed for $target"
}

ensure_swapfile() {
    info "ensuring ${SWAP_SIZE_MB} MiB swapfile at ${SWAPFILE} (priority ${SWAP_PRIO})"
    if [ "$DRY_RUN" != "1" ] && [ ! -e "$SWAPFILE" ]; then
        # Prefer fallocate, but fall back to dd (some filesystems ignore it).
        if ! run fallocate -l "${SWAP_SIZE_MB}M" "$SWAPFILE" 2>/dev/null; then
            local blocks=$(( SWAP_SIZE_MB * 1024 ))
            run dd if=/dev/zero of="$SWAPFILE" bs=1024 count="$blocks" status=none \
                || die "$EXIT_SWAP" "could not create swapfile $SWAPFILE"
        fi
        run chmod 600 "$SWAPFILE"
    elif [ "$DRY_RUN" = "1" ]; then
        info "DRY-RUN: would create $SWAPFILE ($SWAP_SIZE_MB MiB) if missing"
        run chmod 600 "$SWAPFILE"
    fi

    # Make it a swap area once, then activate it if it is not already active.
    if [ "$DRY_RUN" != "1" ]; then
        if ! swaplabel "$SWAPFILE" >/dev/null 2>&1; then
            run mkswap "$SWAPFILE" >/dev/null || die "$EXIT_SWAP" "mkswap failed for $SWAPFILE"
        fi
        if ! is_swap_active "$SWAPFILE"; then
            run swapon --priority "$SWAP_PRIO" "$SWAPFILE" \
                || die "$EXIT_SWAP" "swapon failed for $SWAPFILE"
        else
            # Already active: enforce the priority idempotently (B5).
            ensure_swap_priority "$SWAPFILE" "$SWAP_PRIO"
        fi
    else
        run mkswap "$SWAPFILE"
        run swapon --priority "$SWAP_PRIO" "$SWAPFILE"
    fi
}

persist_swapfile_fstab() {
    # Idempotent /etc/fstab entry so the SSD swapfile survives reboots.
    local entry="${SWAPFILE} none swap sw,pri=${SWAP_PRIO} 0 0"
    if [ "$DRY_RUN" = "1" ]; then
        info "DRY-RUN: would ensure fstab entry: $entry"
        return 0
    fi
    if grep -qsF "$SWAPFILE" /etc/fstab; then
        info "fstab already references $SWAPFILE"
    else
        printf '%s\n' "$entry" >> /etc/fstab
        info "added fstab swap entry for $SWAPFILE"
    fi
}

# ------------------------------------------------------------------- 2) zram
ensure_zram() {
    info "ensuring zram ${ZRAM_SIZE_MB} MiB (priority ${ZRAM_PRIO}) for system pages"
    if [ "$DRY_RUN" != "1" ] && [ -f "$ZRAM_DEFAULTS" ]; then
        run systemctl restart zramswap.service 2>/dev/null || warn "zramswap.service restart failed; will try manual zram"
    fi

    if is_swap_active "/dev/zram0"; then
        info "zram0 already active"
        return 0
    fi

    # Manual fallback: create/format/enable a zram swap device ourselves.
    run modprobe zram num_devices=1 || warn "modprobe zram failed"
    if [ "$DRY_RUN" != "1" ] && [ -e /dev/zram0 ]; then
        run zramctl --find --size "${ZRAM_SIZE_MB}M" >/dev/null || warn "zramctl failed"
        run mkswap /dev/zram0 >/dev/null 2>&1 || true
        run swapon --priority "$ZRAM_PRIO" /dev/zram0 || warn "swapon zram0 failed"
    else
        run zramctl --find --size "${ZRAM_SIZE_MB}M"
        run mkswap /dev/zram0
        run swapon --priority "$ZRAM_PRIO" /dev/zram0
    fi
}

install_zram_defaults() {
    # Ship the tuned zram config (size/prio/algorithm) from the deploy dir.
    local src="${SCRIPT_DIR}/zram-config.conf"
    if [ ! -f "$src" ]; then
        warn "zram-config.conf not found next to script; skipping $ZRAM_DEFAULTS"
        return 0
    fi
    run install -m 0644 "$src" "$ZRAM_DEFAULTS"
}

# ------------------------------------------------------------------ 3) sysctl
install_sysctl() {
    info "installing sysctl drop-in ${SYSCTL_FILE}"
    local src="${SCRIPT_DIR}/sysctl-stratum.conf"
    if [ -f "$src" ]; then
        run install -m 0644 "$src" "$SYSCTL_FILE"
    else
        # Self-contained fallback: still guarantee the normative values.
        if [ "$DRY_RUN" = "1" ]; then
            info "DRY-RUN: would write $SYSCTL_FILE"
        else
            {
                echo "# Stratum-Memory (WP-14) — no source file found, inline values."
                echo "vm.swappiness = 10"
                echo "vm.vfs_cache_pressure = 50"
                echo "vm.overcommit_memory = 0"
            } > "$SYSCTL_FILE"
            chmod 0644 "$SYSCTL_FILE"
        fi
    fi
    run sysctl --system >/dev/null 2>&1 \
        || warn "sysctl --system reported issues; values will apply on next boot"
}

# --------------------------------------------------------------------- main
main() {
    require_root
    require_cmd swapon
    require_cmd awk
    ensure_ssd_mounted
    ensure_swapfile
    persist_swapfile_fstab
    install_zram_defaults
    ensure_zram
    install_sysctl
    info "swap/VM tuning complete (ssd=${SWAPFILE} prio=${SWAP_PRIO}, zram prio=${ZRAM_PRIO})"
    return "$EXIT_OK"
}

main "$@"
