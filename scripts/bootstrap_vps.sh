#!/usr/bin/env bash
# bootstrap_vps.sh — Idempotent Hetzner CX32 setup for trade-alert
# Usage: scp scripts/bootstrap_vps.sh root@<IP>:~ && ssh root@<IP> bash bootstrap_vps.sh
#
# What it does:
#   1. Installs Docker CE + Docker Compose plugin (official apt repo)
#   2. Installs HashiCorp Vault CLI (for vault-init.sh)
#   3. Configures UFW firewall (SSH, HTTP/S, MCP ports 8001-8012)
#   4. Creates non-root 'deploy' user with Docker group access
#   5. Hardens SSH (disable password auth, disable root login)
#
# Safe to re-run — all steps are idempotent.
# No secrets are embedded in this script.
set -euo pipefail

DEPLOY_USER="deploy"

# Shared utilities used by deployment scripts and day-2 operations.
BASE_PACKAGES=(
    apt-transport-https
    ca-certificates
    curl
    git
    gnupg
    jq
    lsb-release
    openssl
    python3
    python3-pip
    python3-venv
    software-properties-common
    ufw
    unzip
    wget
)

echo "==> [0/5] Installing base OS packages"
apt-get update -qq
apt-get install -y -qq "${BASE_PACKAGES[@]}"
echo "    Base packages installed/updated"

echo "==> [1/5] Installing Docker CE + Compose plugin"
if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" \
      | tee /etc/apt/sources.list.d/docker.list > /dev/null

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    echo "    Docker installed: $(docker --version)"
else
    echo "    Docker already installed: $(docker --version)"
fi

echo "==> [2/5] Installing HashiCorp Vault CLI"
if ! command -v vault &>/dev/null; then
    curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
      https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
      | tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
    apt-get update -qq
    apt-get install -y -qq vault
    echo "    Vault CLI installed: $(vault version | head -1)"
else
    echo "    Vault CLI already installed: $(vault version | head -1)"
fi

echo "==> [3/5] Configuring UFW firewall"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw allow 80/tcp comment "HTTP"
ufw allow 443/tcp comment "HTTPS"
ufw allow 8001:8012/tcp comment "MCP servers"
ufw allow 8080/tcp comment "Dashboard API"
ufw --force enable
echo "    UFW active — $(ufw status | grep -c ALLOW) rules configured"

echo "==> [4/5] Creating deploy user"
if ! id "$DEPLOY_USER" &>/dev/null; then
    adduser --disabled-password --gecos "Trade-Alert Deploy" "$DEPLOY_USER"
    usermod -aG docker "$DEPLOY_USER"
    # Copy root's authorized_keys so the same SSH key works for deploy user
    mkdir -p "/home/$DEPLOY_USER/.ssh"
    if [[ -f /root/.ssh/authorized_keys ]]; then
        cp /root/.ssh/authorized_keys "/home/$DEPLOY_USER/.ssh/authorized_keys"
    else
        echo "    WARNING: /root/.ssh/authorized_keys not found — add a key manually"
        touch "/home/$DEPLOY_USER/.ssh/authorized_keys"
    fi
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
    chmod 700 "/home/$DEPLOY_USER/.ssh"
    chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"
    echo "    User '$DEPLOY_USER' created and added to docker group"
else
    usermod -aG docker "$DEPLOY_USER" 2>/dev/null || true
    echo "    User '$DEPLOY_USER' already exists (ensured docker group)"
fi

echo "==> [5/5] Hardening SSH"
SSHD_CONFIG="/etc/ssh/sshd_config"

# Helper: ensure a directive is present with the desired value.
# Uses sed to replace if the directive exists (commented or not),
# otherwise appends to end of file.
_sshd_set() {
    local key="$1" val="$2"
    if grep -qE "^#?${key}\b" "$SSHD_CONFIG"; then
        sed -i "s/^#\?${key}.*/${key} ${val}/" "$SSHD_CONFIG"
    else
        echo "${key} ${val}" >> "$SSHD_CONFIG"
    fi
}

_sshd_set PasswordAuthentication no
_sshd_set PermitRootLogin no
_sshd_set PermitEmptyPasswords no

# Validate config before restarting
if sshd -t 2>/dev/null; then
    systemctl restart sshd
    echo "    SSH hardened (password auth disabled, root login disabled)"
else
    echo "    ERROR: sshd config validation failed — SSH not restarted"
    exit 1
fi

echo ""
echo "=========================================="
echo " Bootstrap complete!"
echo " SSH as: ssh $DEPLOY_USER@$(hostname -I | awk '{print $1}')"
echo " Next: clone repo, configure .env, seed Vault, docker compose up"
echo "=========================================="
