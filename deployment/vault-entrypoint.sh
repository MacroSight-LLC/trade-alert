#!/usr/bin/env sh
# vault-entrypoint.sh — Start Vault server and auto-unseal if init file exists
set -e

# Start Vault server in the background
vault server -config=/vault/config/vault.hcl &
VAULT_PID=$!

HEALTH_URL="http://127.0.0.1:8200/v1/sys/health?standbyok=true&perfstandbyok=true&sealedcode=200&uninitcode=200&activecode=200&standbycode=200&drsecondarycode=200&performancestandbycode=200"

# Wait for Vault to be listening (accepts any HTTP response)
echo "Waiting for Vault to start..."
for i in $(seq 1 30); do
    HTTP_CODE=$(wget --spider -qS "$HEALTH_URL" 2>&1 | grep "HTTP/" | awk '{print $2}' | tail -1 || echo "000")
    if [ "$HTTP_CODE" != "000" ] && [ "$HTTP_CODE" != "" ]; then
        echo "Vault is listening (HTTP $HTTP_CODE)"
        break
    fi
    sleep 1
done

# Auto-unseal if the init file is mounted
INIT_FILE="/vault/config/init.json"
if [ -f "$INIT_FILE" ]; then
    # Query a tolerant health endpoint so sealed state still returns JSON.
    HEALTH=$(wget -qO- "$HEALTH_URL" 2>/dev/null || true)
    IS_SEALED=$(echo "$HEALTH" | grep -o '"sealed":true' || true)
    if [ -n "$IS_SEALED" ]; then
        # Flatten JSON first so sed can match pretty-printed init.json content.
        UNSEAL_KEY=$(tr -d '\n[:space:]' < "$INIT_FILE" | sed -n 's/.*"unseal_keys_b64":\["\([^"]*\)".*/\1/p')
        if [ -n "$UNSEAL_KEY" ]; then
            export VAULT_ADDR=http://127.0.0.1:8200
            vault operator unseal "$UNSEAL_KEY" >/dev/null 2>&1 && echo "Vault auto-unsealed" || echo "Auto-unseal failed"
        fi
    else
        echo "Vault is already unsealed (or not yet initialized)"
    fi
fi

# Wait for the Vault process
wait $VAULT_PID
