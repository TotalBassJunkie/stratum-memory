#!/bin/sh
# Stratum-Memory container entrypoint.
#
# Docker Compose mounts `secrets:` as READ-ONLY, root-owned files whose host
# ownership (e.g. uid 1000) is preserved. The application runs as the
# unprivileged user `stratum` (UID 10001), so it cannot read them directly and
# chown/chmod on the ro mount silently fails.
#
# Fix: as root, copy each secret into a private directory owned by `stratum`,
# repoint the *_FILE env vars at those copies, then drop privileges and exec.
# This keeps the non-root runtime posture and works with plain Compose secrets
# (no Swarm uid/gid/mode support needed).
set -eu

APP_USER=stratum
PRIV_DIR=/run/stratum-secrets

if [ -d /run/secrets ]; then
    mkdir -p "$PRIV_DIR"
    chown "$APP_USER:$APP_USER" "$PRIV_DIR"
    chmod 0700 "$PRIV_DIR"
    for f in /run/secrets/*; do
        [ -e "$f" ] || continue
        # Skip directories or any non-regular file: `cp` on a directory (or an
        # empty entry) would abort the container start with `set -e` (B14).
        [ -f "$f" ] || continue
        name="$(basename "$f")"
        cp "$f" "$PRIV_DIR/$name"
        chown "$APP_USER:$APP_USER" "$PRIV_DIR/$name"
        chmod 0400 "$PRIV_DIR/$name"
    done
    # Repoint the secret-file env vars at the readable copies (STRATUM_ prefix
    # per stratum/config.py).
    [ -e "$PRIV_DIR/app_password" ] && export STRATUM_DB_PASSWORD_FILE="$PRIV_DIR/app_password"
    [ -e "$PRIV_DIR/llm_key" ] && export STRATUM_LLM_API_KEY_FILE="$PRIV_DIR/llm_key"
    [ -e "$PRIV_DIR/ov_user_key" ] && export OPENVIKING_API_KEY_FILE="$PRIV_DIR/ov_user_key"
fi

# The vault bind mount may arrive with foreign ownership; the app user needs it.
[ -d /var/stratum/kb ] && chown -R "$APP_USER:$APP_USER" /var/stratum/kb 2>/dev/null || true
# Working/state dir for the bulk importer (bind-mounted from the host).
[ -d /app/var ] && chown -R "$APP_USER:$APP_USER" /app/var 2>/dev/null || true

exec runuser -u "$APP_USER" -- "$@"