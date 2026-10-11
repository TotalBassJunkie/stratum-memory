#!/usr/bin/env bash
#
# Stratum-Memory — restore (WP-15). Target: Raspberry Pi / Linux (Docker Compose).
#
# Restores a database dump produced by backup.sh into a target database and/or
# extracts a vault tarball. NON-INTERACTIVE: every input arrives as an argument
# or environment variable, so the script is safe inside runbooks and drills.
#
#   restore.sh --dump <file> [--target-db NAME] [--force]
#   restore.sh --dump <file> --verify          # integrity only, no writes
#   restore.sh --vault <file> --target-dir DIR
#
# File-name contract (see docs/backup-restore.md):
#   database dump    .../stratum-????????-??????.dump   (regex: stratum-\d{8}-\d{6}\.dump)
#   vault archive    .../vault-????????-??????.tar.gz   (regex: vault-\d{8}-\d{6}\.tar\.gz)
#
# Exit codes:
#   0  success
#   1  usage / verification error
#   2  dump file missing or unreadable
#   3  pg_restore failed
#   4  vault archive missing or unreadable
#   5  vault extraction failed
#   6  missing dependency (docker / pg_restore / tar)
#
# Environment overrides (flags win):
#   STRATUM_RESTORE_DB_CONTAINER  compose service/container (default stratum-db)
#   STRATUM_RESTORE_DB_USER       role used to connect   (default stratum)
#   STRATUM_RESTORE_DB_URL        local pg_restore URL instead of docker exec
#   STRATUM_RESTORE_DRY_RUN       1 = print commands, write nothing
#   STRATUM_RESTORE_ALLOW_PROD    1 = allow `--target-db stratum --force`
#                                 (otherwise that destructive combination is refused)
#
set -euo pipefail

readonly SCRIPT_NAME="${0##*/}"

readonly EXIT_OK=0
readonly EXIT_USAGE=1
readonly EXIT_DUMP_MISSING=2
readonly EXIT_RESTORE_FAILED=3
readonly EXIT_VAULT_MISSING=4
readonly EXIT_VAULT_EXTRACT=5
readonly EXIT_MISSING_DEP=6

DB_CONTAINER="${STRATUM_RESTORE_DB_CONTAINER:-stratum-db}"
DB_USER="${STRATUM_RESTORE_DB_USER:-stratum}"
DB_URL="${STRATUM_RESTORE_DB_URL:-}"
DRY_RUN="${STRATUM_RESTORE_DRY_RUN:-0}"

DUMP_FILE=""
VAULT_FILE=""
TARGET_DB=""
TARGET_DIR=""
VERIFY=0
FORCE=0

log() {
    local level="$1"; shift
    local stamp
    stamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '%s [%s] %s\n' "$stamp" "$level" "$*" >&2
}

die() {
    local code="$1"; shift
    log ERROR "$*"
    exit "$code"
}

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME --dump FILE [--target-db NAME] [--force]
       $SCRIPT_NAME --dump FILE --verify
       $SCRIPT_NAME --vault FILE --target-dir DIR

Restore options:
  --dump FILE           pg_dump custom-format archive to restore
  --target-db NAME      database to restore INTO (required for a real restore)
  --verify              only check the dump's integrity (pg_restore --list); no writes
  --force               pass --clean --if-exists (drop existing objects first)
  --vault FILE          vault tarball (.tar.gz) to extract
  --target-dir DIR      directory the vault is extracted into (required for --vault)
  --db-container C      compose service/container for psql (default: $DB_CONTAINER)
  -U, --db-user USER    role used to connect          (default: $DB_USER)
  -u, --db-url URL      use a local pg_restore against URL instead of docker exec
      --dry-run         print the work, write nothing
  -h, --help            show this help and exit 0
EOF
}

parse_args() {
    while (($#)); do
        case "$1" in
            --dump)          DUMP_FILE="${2:?--dump needs a value}"; shift 2 ;;
            --vault)         VAULT_FILE="${2:?--vault needs a value}"; shift 2 ;;
            --target-db)     TARGET_DB="${2:?--target-db needs a value}"; shift 2 ;;
            --target-dir)    TARGET_DIR="${2:?--target-dir needs a value}"; shift 2 ;;
            --verify)        VERIFY=1; shift ;;
            --force)         FORCE=1; shift ;;
            --db-container)  DB_CONTAINER="${2:?--db-container needs a value}"; shift 2 ;;
            -U|--db-user)    DB_USER="${2:?--db-user needs a value}"; shift 2 ;;
            -u|--db-url)     DB_URL="${2:?--db-url needs a value}"; shift 2 ;;
            --dry-run)       DRY_RUN=1; shift ;;
            -h|--help)       usage; exit "$EXIT_OK" ;;
            *)               usage >&2; die "$EXIT_USAGE" "unknown option: $1" ;;
        esac
    done

    if [[ -z "$DUMP_FILE" && -z "$VAULT_FILE" ]]; then
        usage >&2
        die "$EXIT_USAGE" "nothing to restore: pass --dump and/or --vault"
    fi
    if [[ -n "$DUMP_FILE" && "$VERIFY" == "0" && -z "$TARGET_DB" ]]; then
        die "$EXIT_USAGE" "--target-db is required for a real restore (or use --verify)"
    fi
    if [[ -n "$VAULT_FILE" && -z "$TARGET_DIR" ]]; then
        die "$EXIT_USAGE" "--target-dir is required to extract a vault archive"
    fi

    # Restoring a dump over the production database with --force drops every
    # existing object first — a data-loss footgun. Refuse it loudly unless the
    # operator explicitly opts in via STRATUM_RESTORE_ALLOW_PROD=1.
    if [[ "$TARGET_DB" == "stratum" && "$FORCE" == "1" && "$VERIFY" == "0" ]]; then
        if [[ "${STRATUM_RESTORE_ALLOW_PROD:-0}" != "1" ]]; then
            die "$EXIT_USAGE" "REFUSING to --force-restore into the production database 'stratum'. \
Pass --target-db <other> for a drill, or set STRATUM_RESTORE_ALLOW_PROD=1 if this is truly intended."
        fi
        log WARN "STRATUM_RESTORE_ALLOW_PROD=1: force-restoring into the production database 'stratum'"
    fi
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$EXIT_MISSING_DEP" "required command not found: $1"
}

require_readable() {
    local file="$1" code="$2"
    [[ -f "$file" ]] || die "$code" "archive not found: $file"
    [[ -r "$file" ]] || die "$code" "archive not readable: $file"
    [[ -s "$file" ]] || die "$code" "archive is empty: $file"
}

# ---------------------------------------------------------------- db restore
pg_restore_argv() {
    # pg_restore_argv FILE DB  -> prints the argv as NUL-separated fields on stdout
    local file="$1" db="$2"
    printf '%s\0' pg_restore --dbname="$db" --no-owner --no-privileges --single-transaction
    if [[ "$FORCE" == "1" ]]; then
        printf '%s\0' --clean --if-exists
    fi
    printf '%s\0' "$file"
}

verify_dump() {
    local file="$1"
    log INFO "verify: $(basename "$file")"
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "pg_restore --list $file"
        return 0
    fi
    if [[ -n "$DB_URL" ]]; then
        pg_restore --list "$file" >/dev/null || die "$EXIT_USAGE" "dump failed integrity check"
    else
        docker exec -i "$DB_CONTAINER" pg_restore --list <"$file" >/dev/null \
            || die "$EXIT_USAGE" "dump failed integrity check"
    fi
    log INFO "verify: OK"
}

restore_dump() {
    local file="$1" db="$2"
    log INFO "restore: $(basename "$file") -> db '$db' (force=$FORCE)"
    if [[ "$DRY_RUN" == "1" ]]; then
        if [[ -n "$DB_URL" ]]; then
            log INFO "pg_restore --dbname=$db --no-owner --no-privileges --single-transaction [--clean --if-exists] $file"
        else
            log INFO "docker exec -i $DB_CONTAINER pg_restore -U $DB_USER -d $db --no-owner --no-privileges --single-transaction [--clean --if-exists] < $file"
        fi
        return 0
    fi

    # --single-transaction makes the restore atomic: a failure leaves the target
    # database untouched instead of half-populated (no transaction block needed).
    if [[ -n "$DB_URL" ]]; then
        # shellcheck disable=SC2046
        local -a argv=()
        while IFS= read -r -d '' item; do argv+=("$item"); done < <(pg_restore_argv "$file" "$DB_URL")
        "${argv[@]}" || die "$EXIT_RESTORE_FAILED" "pg_restore (local) failed"
    else
        local -a argv=(pg_restore -U "$DB_USER" -d "$db" --no-owner --no-privileges --single-transaction)
        if [[ "$FORCE" == "1" ]]; then
            argv+=(--clean --if-exists)
        fi
        # The dump streams in on stdin; a *missing* target DB cannot be created by
        # pg_restore, so create it on demand and continue.
        local createdb_sql="SELECT 1 FROM pg_database WHERE datname = '${db//\'/\'\'}'"
        if ! docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d postgres -tAc "$createdb_sql" 2>/dev/null | grep -q '^1$'; then
            log WARN "target database '$db' does not exist, creating it"
            docker exec -i "$DB_CONTAINER" createdb -U "$DB_USER" "$db" \
                || die "$EXIT_RESTORE_FAILED" "could not create target database '$db'"
        fi
        docker exec -i "$DB_CONTAINER" "${argv[@]}" <"$file" \
            || die "$EXIT_RESTORE_FAILED" "pg_restore via '$DB_CONTAINER' failed"
    fi
    log INFO "restore: OK"
}

# ------------------------------------------------------------- vault restore
restore_vault() {
    local file="$1" dir="$2"
    log INFO "extract vault: $(basename "$file") -> $dir"
    if [[ "$DRY_RUN" == "1" ]]; then
        log INFO "mkdir -p $dir && tar -C $dir -xzf $file"
        return 0
    fi
    mkdir -p "$dir" || die "$EXIT_VAULT_EXTRACT" "cannot create target dir: $dir"
    # Feed the archive on stdin (`-f -`): a Windows path passed to `-f` would be
    # misread by GNU tar as a `host:path` remote spec.
    tar -C "$dir" -xzf - <"$file" || die "$EXIT_VAULT_EXTRACT" "vault extraction failed"
    log INFO "extract: OK"
}

# ------------------------------------------------------------------- main
main() {
    parse_args "$@"

    if [[ -n "$DUMP_FILE" ]]; then
        require_readable "$DUMP_FILE" "$EXIT_DUMP_MISSING"
        if [[ "$DRY_RUN" != "1" ]]; then
            if [[ -z "$DB_URL" ]]; then require_cmd docker; else require_cmd pg_restore; fi
        fi
        verify_dump "$DUMP_FILE"
        if [[ "$VERIFY" == "0" ]]; then
            restore_dump "$DUMP_FILE" "$TARGET_DB"
        else
            log INFO "verify-only: no data written"
        fi
    fi

    if [[ -n "$VAULT_FILE" ]]; then
        require_readable "$VAULT_FILE" "$EXIT_VAULT_MISSING"
        [[ "$DRY_RUN" == "1" ]] || require_cmd tar
        restore_vault "$VAULT_FILE" "$TARGET_DIR"
    fi

    log INFO "=== restore finished ==="
    exit "$EXIT_OK"
}

main "$@"
