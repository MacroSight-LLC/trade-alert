#!/usr/bin/env bash
# bootstrap-remote.sh — Run from your laptop to bootstrap a fresh Hetzner VPS.
#
# Prerequisites:
#   - Hetzner VPS created with your SSH public key added in the cloud console
#   - You can SSH as root:  ssh root@YOUR_IP
#
# Usage:
#   ./scripts/bootstrap-remote.sh YOUR_IP
#   ./scripts/bootstrap-remote.sh YOUR_IP ~/.ssh/id_ed25519
#
# What it does:
#   1. Copies scripts/bootstrap_vps.sh to the VPS
#   2. Runs bootstrap (Docker, Vault CLI, UFW, deploy user, SSH hardening)
#   3. Verifies the deploy user can run docker and vault
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 VPS_IP [SSH_PRIVATE_KEY_PATH]"
    echo ""
    echo "Example:"
    echo "  cd ~/projects/trade-alert"
    echo "  ./scripts/bootstrap-remote.sh 203.0.113.42"
    echo "  ./scripts/bootstrap-remote.sh 203.0.113.42 ~/.ssh/id_ed25519"
    exit 1
fi

IP="$1"
SSH_KEY="${2:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
if [[ -n "$SSH_KEY" ]]; then
    SSH_OPTS+=(-i "$SSH_KEY")
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOOTSTRAP="$REPO_ROOT/scripts/bootstrap_vps.sh"

if [[ ! -f "$BOOTSTRAP" ]]; then
    echo "ERROR: missing $BOOTSTRAP — run this from the trade-alert repo."
    exit 1
fi

echo "==> [1/3] Copy bootstrap script to root@${IP}..."
scp "${SSH_OPTS[@]}" "$BOOTSTRAP" "root@${IP}:~/bootstrap_vps.sh"

echo "==> [2/3] Run bootstrap on VPS..."
ssh "${SSH_OPTS[@]}" "root@${IP}" 'bash ~/bootstrap_vps.sh'

echo "==> [3/3] Verify deploy user..."
ssh "${SSH_OPTS[@]}" "deploy@${IP}" "docker --version && vault version | head -1"

cat <<EOF

========================================
 Bootstrap complete on ${IP}
========================================

Log in as deploy (root SSH is now disabled):

  ssh deploy@${IP}

On the VPS, continue first-time setup:

  git clone https://github.com/MacroSight-LLC/trade-alert.git ~/trade-alert
  cd ~/trade-alert
  git checkout stabilization/v1.2.0-sprint
  cp .env.secrets.example .env.secrets && nano .env.secrets
  cp .env.example .env && nano .env
  ./scripts/deploy.sh --mcp

Sprint prod checkpoints (after stack is healthy):

  VAULT_REQUIRED=true ./deployment/verify-vault-required.sh
  ./deployment/validate-sonnet-4-5.sh
  curl -sf http://localhost:8012/health | jq .

EOF
