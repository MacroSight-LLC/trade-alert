#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# vault-init.sh — Bootstrap HashiCorp Vault for trade-alert
#
# Reads secrets from your existing .env file and writes them
# into Vault KV v2 at  secret/trade-alert  so the pipeline
# can fetch them at runtime instead of relying on env files.
#
# Usage:
#   # 1. Start Vault (docker compose up vault -d)
#   # 2. Run this script:
#   ./scripts/vault-init.sh              # reads .env from repo root
#   ./scripts/vault-init.sh /path/.env   # reads a custom env file
#
# Prerequisites:
#   - vault CLI installed (brew install hashicorp/tap/vault)
#   - VAULT_ADDR and VAULT_TOKEN exported (or defaults used below)
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${1:-$REPO_ROOT/.env}"
VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
VAULT_TOKEN="${VAULT_TOKEN:-dev-root-token}"
VAULT_PATH="secret/trade-alert"

export VAULT_ADDR VAULT_TOKEN

echo "╔══════════════════════════════════════════════════╗"
echo "║  trade-alert — Vault Bootstrap                   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  Vault addr : $VAULT_ADDR"
echo "  Secret path: $VAULT_PATH"
echo "  Env file   : $ENV_FILE"
echo ""

# ── Wait for Vault to be ready ──────────────────────────────
echo "⏳ Waiting for Vault to be ready..."
for i in $(seq 1 30); do
    if vault status >/dev/null 2>&1; then
        echo "✅ Vault is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "❌ Vault did not become ready in 30s"
        exit 1
    fi
    sleep 1
done

# ── Enable KV v2 secrets engine (idempotent) ────────────────
echo ""
echo "🔧 Ensuring KV v2 secrets engine is enabled at 'secret/'..."
vault secrets enable -version=2 -path=secret kv 2>/dev/null \
    && echo "   Enabled KV v2 at secret/" \
    || echo "   KV v2 already enabled at secret/ (OK)"

# ── Read .env and build key=value pairs ──────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "❌ Env file not found: $ENV_FILE"
    echo "   Create a .env file first:  cp .env.example .env"
    exit 1
fi

echo ""
echo "📥 Reading secrets from $ENV_FILE..."

# Parse .env: skip comments, blank lines, and lines without '='
declare -A SECRETS
COUNT=0
while IFS= read -r line || [ -n "$line" ]; do
    # Skip comments and empty lines
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue

    # Must contain '='
    [[ "$line" != *"="* ]] && continue

    KEY="${line%%=*}"
    VALUE="${line#*=}"

    # Strip optional quotes
    VALUE="${VALUE#\"}"
    VALUE="${VALUE%\"}"
    VALUE="${VALUE#\'}"
    VALUE="${VALUE%\'}"

    # Skip empty values and non-secret tunables
    [ -z "$VALUE" ] && continue

    # Only store actual secrets (API keys, passwords, URLs, tokens)
    case "$KEY" in
        POSTGRES_PASSWORD|DISCORD_BOT_TOKEN|DISCORD_WEBHOOK|\
        DISCORD_ALERT_CHANNEL_ID|DISCORD_OPS_CHANNEL_ID|\
        FRED_API_KEY|FINNHUB_API_KEY|ANTHROPIC_API_KEY|\
        POLYGON_API_KEY|REDDIT_CLIENT_ID|REDDIT_CLIENT_SECRET|\
        DATABASE_URL|REDIS_URL|POSTGRES_USER|\
        GROQ_API_KEY|OPENAI_API_KEY|E2B_API_KEY|CUGA_SECRET_KEY)
            SECRETS["$KEY"]="$VALUE"
            COUNT=$((COUNT + 1))
            echo "   ✓ $KEY"
            ;;
        *)
            # Non-secrets (thresholds, tunables) stay in .env
            ;;
    esac
done < "$ENV_FILE"

if [ "$COUNT" -eq 0 ]; then
    echo ""
    echo "⚠️  No secrets found in $ENV_FILE (values may be empty)"
    echo "   Fill in your .env file and re-run this script."
    exit 1
fi

# ── Write to Vault ──────────────────────────────────────────
echo ""
echo "🔐 Writing $COUNT secret(s) to Vault at $VAULT_PATH..."

# Build the vault kv put command with all key=value pairs
KV_ARGS=""
for KEY in "${!SECRETS[@]}"; do
    KV_ARGS="$KV_ARGS ${KEY}=${SECRETS[$KEY]}"
done

vault kv put "$VAULT_PATH" $KV_ARGS

echo ""
echo "✅ All $COUNT secrets written to Vault"
echo ""

# ── Verify ──────────────────────────────────────────────────
echo "🔍 Verifying — listing keys at $VAULT_PATH:"
vault kv get -format=json "$VAULT_PATH" | python3 -c "
import json, sys
data = json.load(sys.stdin).get('data', {}).get('data', {})
for k in sorted(data.keys()):
    v = str(data[k])
    masked = v[:3] + '***' + v[-2:] if len(v) > 8 else '***'
    print(f'   {k} = {masked}')
" 2>/dev/null || echo "   (install python3 for masked output, or run: vault kv get $VAULT_PATH)"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Done! Your secrets are now in Vault.            ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── Generate .env.vault for MCP containers ──────────────────
echo "📄 Generating .env.vault for Docker Compose MCP services..."
python3 "$REPO_ROOT/vault_env_loader.py" --dotenv > "$REPO_ROOT/.env.vault" 2>/dev/null
if [ $? -eq 0 ]; then
    VAULT_COUNT=$(grep -c '=' "$REPO_ROOT/.env.vault" || true)
    echo "   ✅ .env.vault written ($VAULT_COUNT keys)"
else
    echo "   ⚠️  Could not generate .env.vault (run manually: python vault_env_loader.py --dotenv > .env.vault)"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Next steps:                                     ║"
echo "║  1. Set VAULT_ADDR and VAULT_TOKEN in .env       ║"
echo "║  2. Remove the raw secret values from .env       ║"
echo "║  3. docker compose up                            ║"
echo "╚══════════════════════════════════════════════════╝"
