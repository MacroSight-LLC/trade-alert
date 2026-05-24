#!/usr/bin/env bash
# Shared helpers for production deployment scripts on the Hetzner host.
# Usage (from deployment/*.sh):
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=deployment/load-prod-env.sh
#   source "$SCRIPT_DIR/load-prod-env.sh"
#   load_prod_env "$SCRIPT_DIR/.."

load_prod_env() {
    local repo_root
    repo_root="$(cd "${1:-.}" && pwd)"
    cd "$repo_root"

    export DC="${DC:-docker compose -f docker-compose.prod.yml}"

    if [[ -f "$repo_root/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        source "$repo_root/.env"
        if [[ -f "$repo_root/.env.secrets" ]]; then
            # shellcheck disable=SC1091
            source "$repo_root/.env.secrets"
        fi
        set +a
    fi
}

# Docker bind-mount requires a regular file; missing path becomes a directory.
ensure_vault_init_file() {
    local repo_root
    repo_root="$(cd "${1:-.}" && pwd)"
    local init_file="$repo_root/.vault-init.json"

    if [[ -d "$init_file" ]]; then
        echo "ERROR: $init_file is a directory — remove it before starting Vault:"
        echo "  docker run --rm -v ${repo_root}:/mnt alpine rm -rf /mnt/.vault-init.json"
        return 1
    fi
    if [[ ! -f "$init_file" ]]; then
        echo '{}' >"$init_file"
        chmod 600 "$init_file"
    fi
}

dashboard_curl() {
    local url="$1"
    if [[ -n "${DASHBOARD_API_KEY:-}" ]]; then
        curl -sf "$url" -H "X-API-Key: ${DASHBOARD_API_KEY}"
    else
        curl -sf "$url"
    fi
}
