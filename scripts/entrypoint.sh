#!/usr/bin/env bash
# Docker entrypoint — load Vault secrets without shell eval, then exec main process.
set -euo pipefail

case "${DATABASE_URL:-}" in
    ""|*localhost:5432/*|*127.0.0.1:5432/*)
        if [ -n "${POSTGRES_PASSWORD:-}" ]; then
            export DATABASE_URL="postgresql://${POSTGRES_USER:-trade_alert}:${POSTGRES_PASSWORD}@postgres:5432/trade_alert"
            echo "[entrypoint] Using internal postgres service for DATABASE_URL"
        fi
        ;;
esac

if [ -n "${VAULT_ADDR:-}" ] && [ -n "${VAULT_TOKEN:-}" ]; then
    echo "[entrypoint] Loading secrets from Vault (${VAULT_ADDR})..."
    exec python /app/scripts/vault_exec.py "$@"
fi

exec "$@"
