#!/usr/bin/env bash
# Sonnet 4.5 end-to-end validation checklist (FU-002).
# Run on the production host after triggering 15m + 1h pipeline cycles.
#
# Usage:
#   ./deployment/validate-sonnet-4-5.sh
#
# See SETUP_AND_OPERATIONS.md § Sonnet 4.5 End-to-End Validation for full runbook.

set -euo pipefail

DC="docker compose -f docker-compose.prod.yml"
FAILED=0
WARN=0

check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "OK  $name"
  else
    echo "FAIL $name"
    FAILED=$((FAILED + 1))
  fi
}

warn() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "OK  $name"
  else
    echo "WARN $name (manual review required)"
    WARN=$((WARN + 1))
  fi
}

echo "=== Sonnet 4.5 validation checklist (FU-002) ==="
echo ""

# Infrastructure prerequisites
check "Cuga healthy" "$DC ps cuga --format '{{.Health}}' | grep -q healthy"
check "Langfuse reachable" "curl -sf http://localhost:3000/api/public/health"
check "Prometheus reachable" "curl -sf http://localhost:9090/-/healthy"

# Model ID in workflow YAML (code-side confirmation)
check "15m workflow model" "grep -q 'claude-sonnet-4-5' workflows/decision-15m.yaml"
check "1h workflow model" "grep -q 'claude-sonnet-4-5' workflows/decision-1h.yaml"

# PlaybookAlert import smoke test inside container
check "PlaybookAlert import" "$DC exec -T cuga python -c 'from models import PlaybookAlert'"

# Gate-rejection metrics available (manual comparison against historical envelope)
warn "Gate rejection metrics" "curl -sf 'http://localhost:9090/api/v1/query?query=gate_rejections_total' | grep -q result"

# Dashboard summary endpoint
check "Dashboard summary" "curl -sf http://localhost:8080/api/summary"

echo ""
echo "Manual steps still required:"
echo "  1. Run 15m + 1h orchestrators (or wait for cron / use !scan)"
echo "  2. Confirm Langfuse traces show model claude-sonnet-4-5"
echo "  3. Verify zero PlaybookAlert validation failures in decision output"
echo "  4. Compare gate_rejections_total per gate vs 7-day pre-migration median (±15%)"
echo "  5. Record trace IDs in FOLLOW_UPS.md and move FU-002 to Resolved"
echo ""

if [ "$FAILED" -gt 0 ]; then
  echo "Automated checks failed: ${FAILED}. Fix infrastructure before live validation."
  exit 1
fi

if [ "$WARN" -gt 0 ]; then
  echo "Automated checks passed with ${WARN} warning(s). Complete manual steps above."
  exit 0
fi

echo "All automated checks passed. Complete manual steps above to close FU-002."
