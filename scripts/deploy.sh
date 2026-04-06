#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# deploy.sh — One-shot production deploy for trade-alert
#
# Runs all deployment phases in order with health-check waits:
#   1. Pre-flight checks
#   2. Build Docker images
#   3. Start Vault → init + seed secrets
#   4. Start infrastructure (Redis, Postgres, Langfuse)
#   5. Start application (cuga, cron, discord-bot, dashboard)
#   6. Seed Langfuse prompts
#   7. Start MCP services (optional: --mcp)
#
# Usage:
#   ./scripts/deploy.sh              # core stack (no MCPs)
#   ./scripts/deploy.sh --mcp        # full stack with MCP services
#   ./scripts/deploy.sh --skip-build # skip image build (re-deploy)
#
# Prerequisites:
#   - Docker + Docker Compose installed (bootstrap_vps.sh)
#   - Vault CLI installed (bootstrap_vps.sh)
#   - .env.secrets filled in (cp .env.secrets.example .env.secrets)
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.prod.yml"
DC="docker compose -f $COMPOSE_FILE"

ENABLE_MCP=false
SKIP_BUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mcp)        ENABLE_MCP=true; shift ;;
        --skip-build) SKIP_BUILD=true; shift ;;
        -h|--help)
            head -20 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

cd "$REPO_ROOT"

# ── Helpers ──────────────────────────────────────────────────

wait_healthy() {
    local service="$1"
    local max_wait="${2:-120}"
    local elapsed=0
    printf "   Waiting for %-16s" "$service..."
    while [ "$elapsed" -lt "$max_wait" ]; do
        STATUS=$($DC ps "$service" --format '{{.Health}}' 2>/dev/null || echo "unknown")
        if [ "$STATUS" = "healthy" ]; then
            echo " ✅ healthy (${elapsed}s)"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo " ❌ timeout (${max_wait}s)"
    echo "   Last 20 lines of $service logs:"
    $DC logs --tail=20 "$service"
    exit 1
}

section() {
    echo ""
    echo "==> $1"
}

# ── Phase 0: Pre-flight ─────────────────────────────────────

echo "╔══════════════════════════════════════════════════╗"
echo "║  trade-alert — Production Deploy                 ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Docker
if ! command -v docker &>/dev/null; then
    echo "❌ Docker not found. Run: bash scripts/bootstrap_vps.sh"
    exit 1
fi
echo "✅ Docker $(docker --version | grep -oP 'Docker version \K[0-9.]+')"

# Vault CLI
if ! command -v vault &>/dev/null; then
    echo "❌ Vault CLI not found. Run: bash scripts/bootstrap_vps.sh"
    exit 1
fi
echo "✅ Vault CLI $(vault version | head -1)"

# .env.secrets
if [ ! -f "$REPO_ROOT/.env.secrets" ]; then
    echo "❌ .env.secrets not found."
    echo "   Run: cp .env.secrets.example .env.secrets && nano .env.secrets"
    exit 1
fi

# Check that at least POSTGRES_PASSWORD is set (basic sanity)
PG_PASS=$(grep -E '^POSTGRES_PASSWORD=' "$REPO_ROOT/.env.secrets" | head -1 | cut -d= -f2-)
if [ -z "$PG_PASS" ]; then
    echo "❌ POSTGRES_PASSWORD is empty in .env.secrets"
    echo "   Generate one: openssl rand -base64 24"
    exit 1
fi
echo "✅ .env.secrets populated"

# Create .env from example if missing
if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "📄 Creating .env from .env.example..."
    cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
fi
echo "✅ .env present"

# Create logs directory
mkdir -p "$REPO_ROOT/logs"

# ── Phase 1: Build images ───────────────────────────────────

if [ "$SKIP_BUILD" = false ]; then
    section "[1/7] Building Docker images..."
    $DC build --parallel
else
    section "[1/7] Skipping build (--skip-build)"
fi

# ── Phase 2: Start Vault ────────────────────────────────────

section "[2/7] Starting Vault..."
$DC up vault -d
wait_healthy vault 60

# ── Phase 3: Initialize Vault & seed secrets ─────────────────

section "[3/7] Initializing Vault & seeding secrets..."

export VAULT_ADDR=http://127.0.0.1:8200

# Read current token from .env.secrets
VAULT_TOKEN=$(grep -E '^VAULT_TOKEN=' "$REPO_ROOT/.env.secrets" | head -1 | cut -d= -f2- || true)
VAULT_TOKEN="${VAULT_TOKEN:-dev-root-token}"
export VAULT_TOKEN

bash "$REPO_ROOT/scripts/vault-init.sh"

# If vault was freshly initialized, capture new root token and auto-update .env.secrets
if [ -f "$REPO_ROOT/.vault-init.json" ]; then
    NEW_TOKEN=$(python3 -c "import json; print(json.load(open('$REPO_ROOT/.vault-init.json'))['root_token'])" 2>/dev/null || true)
    if [ -n "$NEW_TOKEN" ] && [ "$NEW_TOKEN" != "$VAULT_TOKEN" ]; then
        echo "   📝 Auto-updating VAULT_TOKEN in .env.secrets..."
        if grep -q '^VAULT_TOKEN=' "$REPO_ROOT/.env.secrets"; then
            sed -i "s|^VAULT_TOKEN=.*|VAULT_TOKEN=$NEW_TOKEN|" "$REPO_ROOT/.env.secrets"
        else
            echo "VAULT_TOKEN=$NEW_TOKEN" >> "$REPO_ROOT/.env.secrets"
        fi
        export VAULT_TOKEN="$NEW_TOKEN"
        echo "   ✅ .env.secrets updated with new root token"
    fi
fi

# ── Phase 4: Start infrastructure ───────────────────────────

section "[4/7] Starting infrastructure (Redis, Postgres, Langfuse)..."

$DC up redis postgres langfuse-db -d
wait_healthy redis 60
wait_healthy postgres 60
wait_healthy langfuse-db 60

$DC up langfuse -d
wait_healthy langfuse 90

# ── Phase 5: Start application ───────────────────────────────

section "[5/7] Starting application services..."

$DC up cuga cron discord-bot dashboard -d
wait_healthy cuga 60
wait_healthy dashboard 60

# cron and discord-bot don't expose health endpoints the same way;
# verify they're running (not restarting)
sleep 5
for svc in cron discord-bot; do
    STATE=$($DC ps "$svc" --format '{{.State}}' 2>/dev/null || echo "unknown")
    if [ "$STATE" = "running" ]; then
        echo "   ✅ $svc is running"
    else
        echo "   ⚠️  $svc state: $STATE"
    fi
done

# ── Phase 6: Seed Langfuse prompts ───────────────────────────

section "[6/7] Seeding Langfuse prompts..."

$DC exec -T cuga python scripts/seed_langfuse_prompts.py --host http://langfuse:3000 && {
    echo "   ✅ Prompts seeded"
} || {
    echo "   ⚠️  Prompt seeding failed (non-fatal — may already exist)"
    echo "   Run manually: docker compose -f docker-compose.prod.yml exec cuga python scripts/seed_langfuse_prompts.py --update --host http://langfuse:3000"
}

# ── Phase 7: MCP services (optional) ────────────────────────

section "[7/7] MCP services..."

if [ "$ENABLE_MCP" = true ]; then
    echo "   Starting 11 MCP services..."
    $DC --profile mcp up -d
    echo "   Waiting for MCP services to initialize..."
    sleep 20

    MCP_OK=0
    MCP_FAIL=0
    for port in 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010 8011; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${port}/health" 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ]; then
            MCP_OK=$((MCP_OK + 1))
        else
            MCP_FAIL=$((MCP_FAIL + 1))
            echo "   ⚠️  MCP :${port} → HTTP $HTTP_CODE"
        fi
    done
    echo "   ✅ MCP services: ${MCP_OK}/11 healthy, ${MCP_FAIL}/11 pending"
else
    echo "   Skipped (use --mcp to enable)"
    echo "   Or start later: docker compose -f docker-compose.prod.yml --profile mcp up -d"
fi

# ── Summary ──────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Deploy complete!                                ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
$DC --profile mcp ps 2>/dev/null || $DC ps
echo ""

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo "  Dashboard : http://${HOST_IP}:8080"
echo "  Langfuse  : http://${HOST_IP}:3000"
echo "  Vault UI  : http://${HOST_IP}:8200/ui  (if VAULT_BIND=0.0.0.0)"
echo ""
echo "  Cron logs : docker compose -f docker-compose.prod.yml logs -f cron"
echo "  All logs  : docker compose -f docker-compose.prod.yml logs -f"
echo ""
