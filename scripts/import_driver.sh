#!/usr/bin/env bash
# Resilient OpenViking -> Stratum import driver.
#
# The free embedding provider (freellmapi / gemini-embedding-001) enforces a
# daily quota. Instead of failing, this driver runs the importer and, when a run
# stops making progress (quota exhausted), sleeps and retries. The importer's
# checkpoint file makes every retry resume exactly where the previous run left
# off, so the import completes over hours at zero cost.
#
# Runs ON THE PI, against the local compose project. Logs:
#   import_driver.log  - driver-level attempt/backoff log
#   import_host.log    - the importer's own progress log
#
# Usage:  RETRY_SLEEP=900 bash scripts/import_driver.sh
set -uo pipefail

RETRY_SLEEP="${RETRY_SLEEP:-900}"     # seconds between quota-limited retries
MAX_ATTEMPTS="${MAX_ATTEMPTS:-500}"
TENANT="${TENANT:-default}"
MODE="${MODE:-structure}"
AREAS="${AREAS:-memories skills resources codex privacy sessions}"

DRIVER_LOG="$HOME/stratum/import_driver.log"
IMPORT_LOG="$HOME/stratum/import_host.log"
COMPOSE="docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml"
: > "$DRIVER_LOG"
cd "$HOME/stratum"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" >> "$DRIVER_LOG"; }

log "driver started tenant=$TENANT mode=$MODE retry_sleep=${RETRY_SLEEP}s"
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  log "attempt $attempt: running importer"
  $COMPOSE --profile import run --rm --name stratum-import-run stratum-import \
    python scripts/import_openviking.py --mode "$MODE" --tenant "$TENANT" \
      --areas $AREAS > "$IMPORT_LOG" 2>&1
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
log "driver exiting"