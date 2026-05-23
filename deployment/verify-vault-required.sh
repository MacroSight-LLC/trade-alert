#!/usr/bin/env bash
# Verify VAULT_REQUIRED=true is set and Vault secrets load in production.
# Run on the Hetzner host after updating ~/trade-alert/.env:
#   VAULT_REQUIRED=true
#   docker compose -f docker-compose.prod.yml up -d
#
# Usage: ./deployment/verify-vault-required.sh

set -euo pipefail

DC="docker compose -f docker-compose.prod.yml"
FAILED=0

check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "OK  $name"
  else
    echo "FAIL $name"
    FAILED=$((FAILED + 1))
  fi
}

echo "=== VAULT_REQUIRED production verification ==="

check "VAULT_REQUIRED in cuga" "$DC exec -T cuga printenv VAULT_REQUIRED | grep -qi true"
check "Vault secrets loaded" "$DC exec -T cuga python vault_env_loader.py | grep -q 'Loaded'"
check "Vault health" "curl -sf http://localhost:8200/v1/sys/health"
check "Cuga healthy" "$DC ps cuga --format '{{.Health}}' | grep -q healthy"

echo ""
if [ "$FAILED" -gt 0 ]; then
  echo "Verification failed: ${FAILED} check(s). Ensure VAULT_REQUIRED=true is in .env and Vault is reachable."
  exit 1
fi
echo "All checks passed."
