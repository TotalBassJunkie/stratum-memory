#!/usr/bin/env bash
#
# Stratum-Memory — backup (WP-15). Target: Raspberry Pi / Linux (Docker Compose).
#
# Creates, in one run:
#   1. a custom-format database dump  -> <dir>/stratum-YYYYmmdd-HHMMSS.dump
#   2. a gzip tarball of the Git vault -> <dir>/vault-YYYYmmdd-HHMMSS.tar.gz
# and then applies retention ("keep the newest N, per archive family").
#
# Defaults are aligned with ADR-0009 (data + backups on the USB-3 SSD) and
# docker-compose.yml (service `stratum-db`, db/user `stratum`, vault volume `kb`
# mounted at /var/stratum/kb in `stratum-core`).
#
# Exit codes:
#   0  success
#   1  usage / configuration error
#   2  database dump failed
#   3  vault archive failed
#   4  retention (prune) failed
#   5  missing dependency (docker / pg_dump / tar)
#
# File-name contract (see docs/backup-restore.md):
#   database dump    <dir>/stratum-????????-??????.dump   (regex: stratum-\d{8}-\d{6}\.dump)
#   vault archive    <dir>/vault-????????-??????.tar.gz   (regex: vault-\d{8}-\d{6}\.tar\.gz)
#
# Environment overrides (flags win):
#   STRATUM_BACKUP_DIR          backup target dir      (default /mnt/ssd/stratum/backups)
#   STRATUM_BACKUP_KEEP         archives kept per family (default 14; 0 = keep none)
#   STRATUM_BACKUP_DB_CONTAINER compose service/container (default stratum-db)
#   STRATUM_BACKUP_DB_NAME      database name           (default stratum)
#   STRATUM_BACKUP_DB_USER      database role           (default stratum)
#   STRATUM_BACKUP_DB_URL       if set, use a local pg_dump against this URL
#   STRATUM_BACKUP_VAULT_CONTAINER container exposing the vault (default stratum-core)
#   STRATUM_BACKUP_VAULT_PATH   in-container vault path  (default /var/stratum/kb)
#   STRATUM_BACKUP_VAULT_HOST_DIR if set, tar this host dir instead of docker exec
#   STRATUM_BACKUP_DRY_RUN      1 = print commands, write/delete nothing
#
set -euo pipefail

readonly SCRIPT_NAME="${0##*/}"

# ------------------------------------------------------------------ exit codes
readonly EXIT_OK=0
readonly EXIT_USAGE=1
readonly EXIT_DUMP=2
readonly EXIT_VAULT=3
readonly EXIT_RETENTION=4
readonly EXIT_MISSING_DEP=5

# ------------------------------------------------------------------- defaults
DEST="${STRATUM_BACKUP_DIR:-/mnt/ssd/stratum/backups}"
KEEP="${STRATUM_BACKUP_KEEP:-14}"
DB_CONTAINER="${STRATUM_BACKUP_DB_CONTAINER:-stratum-db}"
DB_NAME="${STRATUM_BACKUP_DB_NAME:-stratum}"
DB_USER="${STRATUM_BACKUP_DB_USER:-stratum}"
DB_URL="${STRATUM_BACKUP_DB_URL:-}"
VAULT_CONTAINER="${STRATUM_BACKUP_VAULT_CONTAINER:-stratum-core}"
VAULT_PATH="${STRATUM_BACKUP_VAULT_PATH:-/var/stratum/kb}"
VAULT_HOST_DIR="${STRATUM_BACKUP_VAULT_HOST_DIR:-}"
DRY_RUN="${STRATUM_BACKUP_DRY_RUN:-0}"

DUMP_GLOB="stratum-????????-??????.dump"
VAULT_GLOB="vault-????????-??????.tar.gz"

# ------------------------------------------------------------------- logging
log() {
    # log LEVEL message...
    local level="$1"; shift
    local stamp
    stamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '%s [%s] %s\n' "$stamp" "$level" "$*" >&2
    if [[ -n "${LOG_FILE:-}" && ! -d "$LOG_FILE" ]]; then
        # Grouping applies the redirection to the whole block, so a missing log
        # directory is swallowed rather than aborting the run under `set -e`.
        { printf '%s [%s] %s\n' "$stamp" "$level" "$*" >>"$LOG_FILE"; } 2>/dev/null || true
    fi
}

die() {
    # die CODE message...
    local code="$1"; shift
    log ERROR "$*"
    exit "$code"
}

# ------------------------------------------------------------------- argument parsing
usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [options]

  -d, --dir DIR         backup target directory (default: $DEST)
  -k, --keep N          archives kept per family   (default: $KEEP)
  -c, --db-container C  compose service for psql   (default: $DB_CONTAINER)
  -n, --db-name NAME    database name              (default: $DB_NAME)
  -U, --db-user USER    database role              (default: $DB_USER)
  -u, --db-url URL      dump via a local pg_dump against URL instead of docker exec
  -V, --vault-path P    in-container vault path    (default: $VAULT_PATH)
      --vault-container C   container exposing the vault (default: $VAULT_CONTAINER)
      --vault-host-dir D    tar host dir D instead of docker exec
      --dry-run             print the work, write/delete nothing
  -h, --help            show this help and exit 0
EOF
}

parse_args() {
    while (($#)); do
        case "$1" in
            -d|--dir)            DEST="${2:?--dir needs a value}"; shift 2 ;;
            -k|--keep)           KEEP="${2:?--keep needs a value}"; shift 2 ;;
            -c|--db-container)   DB_CONTAINER="${2:?--db-container needs a value}"; shift 2 ;;
            -n|--db-name)        DB_NAME="${2:?--db-name needs a value}"; shift 2 ;;
            -U|--db-user)        DB_USER="${2:?--db-user needs a value}"; shift 2 ;;
            -u|--db-url)         DB_URL="${2:?--db-url needs a value}"; shift 2 ;;
            -V|--vault-path)     VAULT_PATH="${2:?--vault-path needs a value}"; shift 2 ;;
            --vault-container)   VAULT_CONTAINER="${2:?--vault-container needs a value}"; shift 2 ;;
            --vault-host-dir)    VAULT_HOST_DIR="${2:?--vault-host-dir needs a value}"; shift 2 ;;
            --dry-run)           DRY_RUN=1; shift ;;
            -h|--help)           usage; exit "$EXIT_OK" ;;
            *)                   usage >&2; die "$EXIT_USAGE" "unknown option: $1" ;;
        esac
    done

    if ! [[ "$KEEP" =~ ^[0-9]+$ ]]; then
        die "$EXIT_USAGE" "keep must be a non-negative integer, got: $KEEP"
    fi
}

# ------------------------------------------------------------------- preflight
require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$EXIT_MISSING_DEP" "required command not found: $1"
}

preflight() {
    require_cmd date
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "dry-run: docker/pg_dump/tar are not required"
        log INFO "target dir: $DEST (keep=$KEEP, dry_run=$DRY_RUN)"
        return 0
    fi
    require_cmd tar
    if [[ -n "$DB_URL" ]]; then
        require_cmd pg_dump
    else
        require_cmd docker
    fi
    log INFO "target dir: $DEST (keep=$KEEP, dry_run=$DRY_RUN)"
}

# ------------------------------------------------------------------- steps
make_dest() {
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "mkdir -p $DEST"
        return 0
    fi
    mkdir -p "$DEST" || die "$EXIT_USAGE" "cannot create target dir: $DEST"
}

dump_database() {
    local out="$1"
    local tmp="${out}.partial"
    if [[ -n "$DB_URL" ]]; then
        log INFO "pg_dump (local) -> $(basename "$out")"
        if [[ "$DRY_RUN" == "1" ]]; then
            log INFO "pg_dump --dbname=*** --format=custom --no-owner --no-privileges > $out"
            return 0
        fi
        pg_dump --dbname="$DB_URL" --format=custom --no-owner --no-privileges \
            >"$tmp" || die "$EXIT_DUMP" "pg_dump (local) failed"
    else
        log INFO "pg_dump via container '$DB_CONTAINER' -> $(basename "$out")"
        if [[ "$DRY_RUN" == "1" ]]; then
            log INFO "docker exec $DB_CONTAINER pg_dump -U $DB_USER -d $DB_NAME --format=custom --no-owner --no-privileges > $out"
            return 0
        fi
        docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
            --format=custom --no-owner --no-privileges \
            >"$tmp" || die "$EXIT_DUMP" "pg_dump via '$DB_CONTAINER' failed"
    fi
    # A pg_dump custom-format archive always begins with the 5-byte magic
    # "PGDMP". Requiring a non-empty file carrying that header keeps a
    # half-written or truncated dump from ever taking the canonical name
    # (pg_restore's list mode would reject it anyway).
    if [[ ! -s "$tmp" ]]; then
        rm -f "$tmp"
        die "$EXIT_DUMP" "dump is empty: $tmp"
    fi
    local magic=""
    IFS= read -r -n 5 magic <"$tmp" || true
    if [[ "$magic" != "PGDMP" ]]; then
        rm -f "$tmp"
        die "$EXIT_DUMP" "dump is not a pg_dump custom-format archive (bad magic): $tmp"
    fi
    mv -f "$tmp" "$out" || die "$EXIT_DUMP" "cannot finalise dump: $out"
    log INFO "wrote $(basename "$out") ($(du -h "$out" 2>/dev/null | cut -f1 || echo '?')B)"
}

backup_vault() {
    local out="$1"
    local tmp="${out}.partial"
    if [[ -n "$VAULT_HOST_DIR" ]]; then
        log INFO "tar (host) $VAULT_HOST_DIR -> $(basename "$out")"
        if [[ "$DRY_RUN" == "1" ]]; then
            log INFO "tar -C $VAULT_HOST_DIR -czf $out ."
            return 0
        fi
        [[ -d "$VAULT_HOST_DIR" ]] || die "$EXIT_VAULT" "vault host dir not found: $VAULT_HOST_DIR"
        # `--` and the archive-named-`-` form keep GNU tar from reading a drive
        # letter (e.g. `C:\...`) as a remote host spec, and from treating a
        # leading dash in a path as an option.
        tar -C "$VAULT_HOST_DIR" -czf - -- . >"$tmp" \
            || die "$EXIT_VAULT" "tar of $VAULT_HOST_DIR failed"
    else
        log INFO "tar via container '$VAULT_CONTAINER' ($VAULT_PATH) -> $(basename "$out")"
        if [[ "$DRY_RUN" == "1" ]]; then
            log INFO "docker exec $VAULT_CONTAINER tar -C $VAULT_PATH -czf - . > $out"
            return 0
        fi
        docker exec "$VAULT_CONTAINER" tar -C "$VAULT_PATH" -czf - . \
            >"$tmp" || die "$EXIT_VAULT" "vault tar via '$VAULT_CONTAINER' failed"
    fi
    if [[ ! -s "$tmp" ]]; then
        rm -f "$tmp"
        die "$EXIT_VAULT" "vault archive is empty: $tmp"
    fi
    mv -f "$tmp" "$out" || die "$EXIT_VAULT" "cannot finalise vault archive: $out"
    log INFO "wrote $(basename "$out") ($(du -h "$out" 2>/dev/null | cut -f1 || echo '?')B)"
}

# stamp_is_valid STAMP
#
# True (0) only for a calendar-plausible `YYYYmmdd-HHMMSS` stamp: month 01-12,
# day within the real month length (leap years honoured), and a clock in range.
# Mirrors the `datetime.strptime` validation of scripts/backup_lib.py.
stamp_is_valid() {
    local stamp="$1"
    if [[ ! "$stamp" =~ ^([0-9]{4})([0-9]{2})([0-9]{2})-([0-9]{2})([0-9]{2})([0-9]{2})$ ]]; then
        return 1
    fi
    local year=$((10#${BASH_REMATCH[1]})) month=$((10#${BASH_REMATCH[2]})) day=$((10#${BASH_REMATCH[3]}))
    local hour=$((10#${BASH_REMATCH[4]})) minute=$((10#${BASH_REMATCH[5]})) second=$((10#${BASH_REMATCH[6]}))
    (( year >= 1 )) || return 1
    (( month >= 1 && month <= 12 )) || return 1
    (( hour <= 23 && minute <= 59 && second <= 59 )) || return 1
    local -a dmax=(31 28 31 30 31 30 31 31 30 31 30 31)
    if (( month == 2 )) && (( (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 )); then
        dmax[1]=29
    fi
    (( day >= 1 && day <= dmax[month - 1] )) || return 1
    return 0
}

# prune_family PREFIX SUFFIX KEEP
#
# Keeps the newest KEEP archives of one family. Only files whose *entire* name
# matches the canonical contract are candidates — `stratum-\d{8}-\d{6}\.dump`
# or `vault-\d{8}-\d{6}\.tar\.gz` — and the embedded stamp must be a plausible
# calendar date. Anything else (a hand-made `stratum-manual.dump`, a mangled
# stamp such as `stratum-20261332-250000.dump`, logs, READMEs) is ignored and
# never deleted, so it can never displace a real backup from a keep slot. The
# stamp is fixed-width, so `LC_ALL=C sort -r` is chronological; this mirrors
# scripts/backup_lib.py::select_backups_to_prune() (which is authoritative).
prune_family() {
    local prefix="$1" suffix="$2" keep="$3"
    local -a files=() sorted=()
    local f name i count removed=0
    # Escape regex metacharacters in the suffix (`.tar.gz` -> `\.tar\.gz`) so the
    # dot is literal; the prefix ("stratum-", "vault-") is likewise literal.
    local esc_suffix="${suffix//./\\.}"
    # Anchored contract with the stamp captured: `^stratum-(\d{8}-\d{6})\.dump$`.
    local name_re="^${prefix}([0-9]{8}-[0-9]{6})${esc_suffix}\$"

    shopt -s nullglob
    for f in "$DEST/$prefix"*"$suffix"; do
        files+=("${f##*/}")
    done
    shopt -u nullglob

    # Keep only canonical names with a valid stamp; everything else is left alone.
    local -a stamped=()
    for name in "${files[@]}"; do
        if [[ "$name" =~ $name_re ]] && stamp_is_valid "${BASH_REMATCH[1]}"; then
            stamped+=("$name")
        fi
    done

    count=${#stamped[@]}
    if (( count == 0 )); then
        log INFO "retention $prefix*$suffix: 0 archives, nothing to prune"
        return 0
    fi
    if (( count <= keep )); then
        log INFO "retention $prefix*$suffix: $count <= $keep, nothing to prune"
        return 0
    fi

    while IFS= read -r name; do
        [[ -n "$name" ]] && sorted+=("$name")
    done < <(printf '%s\n' "${stamped[@]}" | LC_ALL=C sort -r)

    for (( i = keep; i < ${#sorted[@]}; i++ )); do
        name="${sorted[i]}"
        if [[ "$DRY_RUN" == "1" ]]; then
            log INFO "rm -f $DEST/$name"
        else
            rm -f "$DEST/$name" || { log WARN "could not remove $name"; continue; }
        fi
        removed=$(( removed + 1 ))
    done
    log INFO "retention $prefix*$suffix: removed $removed, kept $(( ${#sorted[@]} - removed ))"
    return 0
}

apply_retention() {
    prune_family "stratum-" ".dump" "$KEEP" || die "$EXIT_RETENTION" "retention failed (dumps)"
    prune_family "vault-" ".tar.gz" "$KEEP" || die "$EXIT_RETENTION" "retention failed (vaults)"
}

# ------------------------------------------------------------------- main
main() {
    parse_args "$@"
    make_dest
    LOG_FILE="${STRATUM_BACKUP_LOG:-$DEST/backup.log}"
    local stamp
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    local dump="$DEST/stratum-$stamp.dump"
    local vault="$DEST/vault-$stamp.tar.gz"

    log INFO "=== Stratum backup $stamp starting ==="
    preflight
    dump_database "$dump"
    backup_vault "$vault"
    apply_retention
    log INFO "=== Stratum backup finished (dump=$(basename "$dump"), vault=$(basename "$vault")) ==="
    exit "$EXIT_OK"
}

main "$@"
