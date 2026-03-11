#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# langfuse-setup.sh — First-run bootstrap for self-hosted Langfuse
#
# Waits for the Langfuse container to be healthy, then prints
# setup instructions. Run once after `docker compose up`.
#
# Usage:
#   ./scripts/langfuse-setup.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail

LANGFUSE_URL="${LANGFUSE_HOST:-http://localhost:3000}"
HEALTH_URL="${LANGFUSE_URL}/api/public/health"

echo "╔══════════════════════════════════════════════════╗"
echo "║  trade-alert — Langfuse Setup                    ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  Langfuse URL: $LANGFUSE_URL"
echo ""

# ── Wait for Langfuse to be healthy ─────────────────────────
echo "⏳ Waiting for Langfuse to be ready..."
for i in $(seq 1 60); do
    if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
        echo "✅ Langfuse is ready!"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "❌ Langfuse did not become ready in 60s."
        echo "   Check logs: docker compose logs langfuse"
        exit 1
    fi
    sleep 1
done

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Next Steps                                      ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  1. Open Langfuse UI:  $LANGFUSE_URL"
echo "  2. Create an account (first user becomes admin)"
echo "  3. Go to Settings → API Keys → Create API Key"
echo "  4. Copy the public and secret keys to your .env:"
echo ""
echo "     LANGFUSE_PUBLIC_KEY=pk-lf-..."
echo "     LANGFUSE_SECRET_KEY=sk-lf-..."
echo ""
echo "  5. If using Vault, re-run: ./scripts/vault-init.sh"
echo "  6. Restart the pipeline: docker compose restart app"
echo ""
echo "  Traces will appear in $LANGFUSE_URL/traces"
echo "  after the next orchestrator run."
echo ""
