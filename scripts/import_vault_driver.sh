#!/usr/bin/env bash
# Resilient local-vault import driver (mirrors import_driver.sh for OpenViking).
#
# The free embedding provider (freellmapi / gemini-embedding-001) enforces a
# daily quota. This driver re-runs the vault importer whenever a run stops
# making progress; the importer's checkpoint makes every retry resume exactly
# where it left off. Completes over hours at zero cost.
#
# Usage (on the Pi):  RETRY_SLEEP=900 bash scripts/import_vault_driver.sh
set -uo pipefail

RETRY_SLEEP="${RETRY_SLEEP:-900}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-500}"
TENANT="${TENANT:-julian}"
VAULT="${VAULT:-/vault}"

DRIVER_LOG="$HOME/stratum/vault_driver.log"
IMPORT_LOG="$HOME/stratum/vault_import.log"
COMPOSE="docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml"
: > "$DRIVER_LOG"
cd "$HOME/stratum"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$DRIVER_LOG"; }

log "vault driver started tenant=$TENANT vault=$VAULT retry_sleep=${RETRY_SLEEP}s"
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  log "attempt $attempt: running importer"
  $COMPOSE --profile import run --rm --name stratum-vault-import \
    -e STRATUM_IMPORT_STATE=/app/var/import_vault.state \
    stratum-import python scripts/import_vault.py "$VAULT" \
      --mode structure --tenant "$TENANT" > "$IMPORT_LOG" 2>&1
  rc=$?
  summary=$(grep 'DONE:' "$IMPORT_LOG" | tail -1)
  log "attempt $attempt done rc=$rc ${summary:-<no DONE line>}"
  if echo "$summary" | grep -q 'failed=0'; then
    log "clean completion; driver exiting"
    break
  fi
  log "quota/partial; sleeping ${RETRY_SLEEP}s"
  sleep "$RETRY_SLEEP"
done
log "vault driver exiting"