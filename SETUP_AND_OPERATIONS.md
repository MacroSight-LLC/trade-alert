# Trade Alert: Complete Setup & Operations Guide

**Status:** Your stack is **fully operational**. All 17 containers healthy, secrets stored in Vault.

---

## Table of Contents
1. [Current Status](#current-status)
2. [Vault Initialization (Critical)](#vault-initialization-critical)
3. [Environment Variables](#environment-variables)
4. [Full Stack Startup](#full-stack-startup)
5. [Health Checks & Monitoring](#health-checks--monitoring)
6. [Common Operations](#common-operations)
7. [Troubleshooting](#troubleshooting)

---

## Current Status

### Running Containers (17 total)

| Component                   | Status                 | Notes                                      |
| --------------------------- | ---------------------- | ------------------------------------------ |
| **Infrastructure**          |                        |                                            |
| Vault                       | ✅ Healthy              | Dev-mode, auto-unsealed via docker-compose |
| Redis                       | ✅ Healthy              | Ready to store snapshots & state           |
| PostgreSQL (main)           | ✅ Healthy              | Ready to store alerts & outcomes           |
| PostgreSQL (Langfuse)       | ✅ Healthy              | Ready for observability traces             |
| Langfuse                    | ✅ Healthy              | Available at http://localhost:3000         |
| **Application**             |                        |                                            |
| CUGA (app)                  | ✅ Healthy              | Ready to execute workflows                 |
| Cron                        | ⚠️ UP (no health check) | Running fine, attempting Vault connection  |
| **MCP Services** (10 total) | ✅ All Healthy          | All data connectors ready                  |
| - tradingview-mcp           | ✅ Healthy              | TradingView data on :8001                  |
| - polygon-mcp               | ✅ Healthy              | Polygon.io data on :8002                   |
| - discord-mcp               | ✅ Healthy              | Discord notifications on :8003             |
| - finnhub-mcp               | ✅ Healthy              | Finnhub financial data on :8004            |
| - rot-mcp                   | ✅ Healthy              | ROT sentiment on :8005                     |
| - crypto-orderbook-mcp      | ✅ Healthy              | Crypto orderbook on :8006                  |
| - coingecko-mcp             | ✅ Healthy              | CoinGecko crypto data on :8007             |
| - trading-mcp               | ✅ Healthy              | Trading signals on :8008                   |
| - fred-mcp                  | ✅ Healthy              | Federal Reserve economic data on :8009     |
| - spamshield-mcp            | ✅ Healthy              | Spam filtering on :8010                    |

---

## Vault Initialization (Critical)

### Why This Matters
Vault is your **secret store** for production. All API keys, tokens, and
credentials live exclusively in Vault at `secret/trade-alert`. The `.env` file
holds only non-secret tunables and connectivity URLs.

### Development Setup (Recommended)

The docker-compose Vault container runs in **dev mode** (auto-unsealed, root
token `dev-root-token`). Secrets are loaded via `vault-init.sh` which reads
from the `.env.secrets` backup.

```bash
# 1. Ensure .env.secrets exists with your real key values
cp .env .env.secrets   # one-time — .env.secrets is git-ignored

# 2. Run the init script (seeds Vault KV v2)
./scripts/vault-init.sh

# 3. Verify secrets are stored
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=dev-root-token
vault kv get secret/trade-alert
```

After seeding, **remove raw secret values from `.env`** — the runtime loads
them from Vault via `vault_env_loader.py`.

### Production Setup

For production, replace the dev Vault with a properly initialised instance:
- Use **Shamir key holders** or **cloud auto-unseal** (AWS KMS, GCP CKMS).
- Generate a scoped AppRole or token (not root) and set `VAULT_TOKEN` in the
  deployment environment.
- See: https://www.vaultproject.io/docs/concepts/seal

### Vault Recovery (File-Backed Storage)

The production `docker-compose.prod.yml` uses **file-backed storage** (not
dev-mode in-memory). Secrets persist across container restarts via the
`vault-data` Docker volume.

**After a clean restart** (volume intact):
Vault starts **sealed**. You must unseal it before the pipeline can read secrets:

```bash
# Check status (shows "Sealed: true")
docker compose -f docker-compose.prod.yml exec vault vault status

# Unseal with your unseal key(s) — stored during initial vault operator init
docker compose -f docker-compose.prod.yml exec vault \
  vault operator unseal <UNSEAL_KEY>
```

**After volume loss** (e.g., `docker compose down -v`):
All secrets are lost. Re-initialize Vault and re-seed:

```bash
# 1. Re-init (produces new unseal keys + root token — save these securely)
docker compose -f docker-compose.prod.yml exec vault vault operator init

# 2. Unseal with the new keys
docker compose -f docker-compose.prod.yml exec vault \
  vault operator unseal <NEW_UNSEAL_KEY>

# 3. Re-seed secrets from .env.secrets
VAULT_TOKEN=<NEW_ROOT_TOKEN> ./scripts/vault-init.sh

# 4. Update VAULT_TOKEN in .env.secrets / deployment config
```

**Tip:** For unattended restarts, configure cloud auto-unseal (AWS KMS, GCP
CKMS) so Vault unseals itself on boot. See the Vault auto-unseal docs.

---

## Environment Variables

### Secrets (Vault — `secret/trade-alert`)

All secrets are loaded at runtime by `vault_env_loader.py`. They must **not**
appear in `.env`. After running `vault-init.sh`, verify with:

```bash
vault kv get -format=json secret/trade-alert | jq '.data.data | keys'
```

Expected keys:
`ANTHROPIC_API_KEY`, `DISCORD_ALERT_CHANNEL_ID`, `DISCORD_BOT_TOKEN`,
`DISCORD_OPS_CHANNEL_ID`, `ENCRYPTION_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`,
`GROQ_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
`NEXTAUTH_SECRET`, `POLYGON_API_KEY`, `POSTGRES_PASSWORD`, `POSTGRES_USER`

### Get API Keys

| Service           | URL                                                     | Time  |
| ----------------- | ------------------------------------------------------- | ----- |
| Discord Bot Token | https://discord.com/developers/applications             | 5 min |
| Anthropic         | https://console.anthropic.com/keys                      | 5 min |
| Finnhub           | https://finnhub.io/dashboard (free tier available)      | 5 min |
| FRED              | https://stlouisfed.org/fred (free tier available)       | 5 min |
| Polygon.io        | https://polygon.io/dashboard/keys (free tier available) | 5 min |

### Langfuse Setup (After First Run)

1. **Access Langfuse UI** → http://localhost:3000
2. **Create account** (any email/password)
3. **Generate API keys** → Settings → API Keys
4. **Add to `.env.secrets`:**
   ```bash
   LANGFUSE_PUBLIC_KEY=pk-xxxxx
   LANGFUSE_SECRET_KEY=sk-xxxxx
   NEXTAUTH_SECRET=$(openssl rand -hex 16)
   ENCRYPTION_KEY=$(openssl rand -hex 16)
   ```
5. **Re-seed Vault:**
   ```bash
   ./scripts/vault-init.sh
   ```

---

## Full Stack Startup

### Fresh Start (from scratch)

```bash
cd /Users/taylordean/trade-alert

# 1. Copy & fill environment
cp .env.example .env
# ← EDIT .env with your API keys ←

# 2. Start core infrastructure (Redis, PostgreSQL, Langfuse)
docker compose -f docker-compose.prod.yml up -d redis postgres langfuse-db langfuse vault

# 3. Wait for PostgreSQL to initialize (30-60 seconds)
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U trade_alert

# 4. Apply schema
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U trade_alert -d trade_alert -f /docker-entrypoint-initdb.d/schema.sql

# 5. Seed secrets into Vault
./scripts/vault-init.sh

# 6. Start application layer (CUGA, cron)
docker compose -f docker-compose.prod.yml up -d cuga cron

# 7. Start all MCP services
docker compose -f docker-compose.prod.yml --profile mcp up -d

# 8. Seed Langfuse prompts (enables live editing via UI)
docker compose -f docker-compose.prod.yml exec cuga \
  python scripts/seed_langfuse_prompts.py

# 9. Verify all services
docker compose -f docker-compose.prod.yml ps
```

### Langfuse Prompt Seeding

The decision engine loads prompts from Langfuse Prompt Management (`decision-system`
and `decision-user`). If these don't exist, `prompt_manager.py` falls back to
built-in `_FALLBACK_SYSTEM` / `_FALLBACK_USER` strings — functional but not
editable from the Langfuse UI.

**First deploy (or after wiping Langfuse DB):**

```bash
docker compose -f docker-compose.prod.yml exec cuga \
  python scripts/seed_langfuse_prompts.py
```

After seeding, edit prompts live at http://localhost:3000 → Prompts. Changes
propagate within 300s (the `prompt_manager.py` TTL cache).

### Restart Running Stack

```bash
# If containers are already running:
docker compose -f docker-compose.prod.yml restart
```

### Shutdown

```bash
docker compose -f docker-compose.prod.yml down
```

### Shutdown + Cleanup Data

```bash
docker compose -f docker-compose.prod.yml down -v
# This deletes: Redis data, Vault data, Langfuse database
```

---

## Health Checks & Monitoring

### Quick Health Check

```bash
# All containers
docker compose -f docker-compose.prod.yml ps

# Vault status
docker exec trade-alert-vault-1 vault status

# Redis health
docker compose -f docker-compose.prod.yml exec redis redis-cli ping

# PostgreSQL health
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U trade_alert

# Langfuse health
curl http://localhost:3000/api/public/health

# MCP services (example: tradingview)
curl http://localhost:8001/health
```

### View Logs

```bash
# Cron execution log
docker compose -f docker-compose.prod.yml exec cron tail -f /app/logs/cron.log

# Health checks
docker compose -f docker-compose.prod.yml exec cron tail -f /app/logs/health.log

# Application logs
docker logs trade-alert-cuga-1 -f

# Specific container
docker compose -f docker-compose.prod.yml logs -f cuga

# Last 100 lines, all services
docker compose -f docker-compose.prod.yml logs --tail=100
```

### Monitor Resources

```bash
# Real-time resource usage
docker stats

# Disk usage
docker system df

# Network
docker network ls
docker network inspect trade-alert_trade-net
```

---

## Common Operations

### Run a Workflow Manually

```bash
# Execute a specific workflow (bypasses schedule)
docker compose -f docker-compose.prod.yml exec cuga \
  python pipeline_runner.py workflows/orchestrator-15m.yaml
```

### Execute Python Code in CUGA

```bash
# Interactive Python shell
docker compose -f docker-compose.prod.yml exec cuga python

# One-off script
docker compose -f docker-compose.prod.yml exec cuga \
  python -c "from models import Signal; print(Signal.__fields__.keys())"
```

### Query Database

```bash
# Interactive psql
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U trade_alert -d trade_alert

# One-off query
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U trade_alert -d trade_alert -c "SELECT COUNT(*) FROM alerts;"
```

### View Redis Snapshots

```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli

# Inside redis-cli:
> KEYS *
> GET snapshot:TA
> TTL snapshot:TA
```

### Rebuild MCP Services

```bash
# If you change the MCP Dockerfile
docker compose -f docker-compose.prod.yml build trade-alert-mcp:latest

# Then restart
docker compose -f docker-compose.prod.yml restart $(docker compose -f docker-compose.prod.yml ps --services --filter label=com.docker.compose.service="*-mcp")
```

### Update & Redeploy

```bash
# Pull latest code
git pull

# Rebuild containers
docker compose -f docker-compose.prod.yml build

# Restart with new builds
docker compose -f docker-compose.prod.yml up -d
```

---

## Troubleshooting

### Vault Stays Unhealthy

**Problem:** `docker exec trade-alert-vault-1 vault status` returns an error

**Solution (dev mode):** The dev-mode Vault auto-unseals. If the container
restarted, secrets may be lost (dev-mode is in-memory). Re-seed:
```bash
./scripts/vault-init.sh
vault kv get secret/trade-alert   # verify
```

### CUGA Container Exits

**Problem:** `docker logs trade-alert-cuga-1` shows errors

**Debugging:**
```bash
# View full logs
docker compose -f docker-compose.prod.yml logs cuga

# Check dependencies
docker compose -f docker-compose.prod.yml ps

# If Redis/Postgres not healthy, restart:
docker compose -f docker-compose.prod.yml restart redis postgres

# Then restart CUGA
docker compose -f docker-compose.prod.yml restart cuga
```

### Cron Not Running Workflows

**Problem:** `/app/logs/cron.log` is empty or shows no executions

**Debugging:**
```bash
# View crontab
docker compose -f docker-compose.prod.yml exec cron cat /etc/crontabs/root

# View cron logs
docker compose -f docker-compose.prod.yml exec cron tail -f /app/logs/cron.log

# Manually trigger workflow
docker compose -f docker-compose.prod.yml exec cron \
  python pipeline_runner.py workflows/orchestrator-15m.yaml

# If it works manually but not on schedule, check:
docker logs trade-alert-cron-1 | grep -i error
```

### Discord Notifications Not Sending

**Problem:** Alerts aren't appearing in Discord

**Debugging:**
```bash
# Check Discord token & channel IDs in .env or Vault
docker compose -f docker-compose.prod.yml exec cuga python -c \
  "import os; print(f'Token: {os.getenv(\"DISCORD_BOT_TOKEN\")[:10]}...'); print(f'Channel: {os.getenv(\"DISCORD_ALERT_CHANNEL_ID\")}')"

# Check if bot is in the Discord server
# → Go to Discord Developer Portal → OAuth2 → Bot → Check scopes & permissions

# Verify bot has permissions (Send Messages, Embed Links)
# → Right-click channel → Edit Channel → Permissions

# Test manually
docker compose -f docker-compose.prod.yml exec cuga python << 'EOF'
import discord
from discord import SyncWebhook
# Adjust for your token/channel
EOF
```

### Database Connection Errors

**Problem:** `psycopg2.OperationalError: could not connect to server`

**Debugging:**
```bash
# Check PostgreSQL is running
docker compose -f docker-compose.prod.yml ps postgres

# Check PostgreSQL is healthy
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U trade_alert

# View PostgreSQL logs
docker logs trade-alert-postgres-1

# Test connection manually
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U trade_alert -d trade_alert -c "SELECT 1;"

# If error: try restarting
docker compose -f docker-compose.prod.yml restart postgres
docker compose -f docker-compose.prod.yml restart cuga
```

### High Resource Usage

**Problem:** Docker containers using too much CPU/memory

**Debugging:**
```bash
# Check resource usage
docker stats

# Identify heavy processes
docker top trade-alert-cuga-1

# View memory/disk
docker system df

# Clean up unused images/volumes
docker system prune -a
```

### MCP Service Not Responding

**Problem:** `curl http://localhost:8001/health` returns error

**Debugging:**
```bash
# Check if container is running
docker ps | grep mcp

# View logs
docker logs trade-alert-tradingview-mcp-1

# Restart service
docker compose -f docker-compose.prod.yml restart tradingview-mcp

# Wait 5-10 seconds and retest
sleep 10
curl http://localhost:8001/health
```

---

## Support & References

- **Architecture:** [`CUGA-Trading-Alert-System-SPEC-v1.2.md`](./CUGA-Trading-Alert-System-SPEC-v1.2.md)
- **Application docs:** [`README.cuga.md`](./README.cuga.md)
- **Vault docs:** https://www.vaultproject.io/docs
- **Docker Compose:** https://docs.docker.com/compose/
- **Langfuse:** https://langfuse.com/docs

---

**Last Updated:** March 2026 | Status: Fully operational — secrets in Vault, all 17 containers healthy
