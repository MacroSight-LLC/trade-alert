#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# entrypoint.sh — Docker entrypoint for trade-alert containers.
#
# If VAULT_ADDR and VAULT_TOKEN are set, fetches all secrets from
# Vault and exports them into the shell environment BEFORE the
# main process starts. This ensures CUGA's LLM layer, pipeline
# modules, and any child process all see Vault-sourced secrets.
#
# Falls back silently if Vault is not configured or unreachable.
# ──────────────────────────────────────────────────────────────
set -e

if [ -n "$VAULT_ADDR" ] && [ -n "$VAULT_TOKEN" ]; then
    echo "[entrypoint] Loading secrets from Vault ($VAULT_ADDR)..."
    EXPORTS=$(python /app/vault_env_loader.py --export 2>/dev/null) || true
    if [ -n "$EXPORTS" ]; then
        eval "$EXPORTS"
        COUNT=$(echo "$EXPORTS" | grep -c '^export ' || true)
        echo "[entrypoint] Injected $COUNT secret(s) from Vault"
    else
        echo "[entrypoint] No secrets loaded from Vault — using env vars"
    fi
fi

exec "$@"
