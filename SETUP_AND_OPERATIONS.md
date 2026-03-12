# Trade Alert: Complete Setup & Operations Guide

**Status:** Your stack is **mostly running**. Vault needs initialization, then everything works.

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

| Component                   | Status                 | Notes                                        |
| --------------------------- | ---------------------- | -------------------------------------------- |
| **Infrastructure**          |                        |                                              |
| Vault                       | ❌ **UNHEALTHY**        | Needs initialization (unsealing) — see below |
| Redis                       | ✅ Healthy              | Ready to store snapshots & state             |
| PostgreSQL (main)           | ✅ Healthy              | Ready to store alerts & outcomes             |
| PostgreSQL (Langfuse)       | ✅ Healthy              | Ready for observability traces               |
| Langfuse                    | ✅ Healthy              | Available at http://localhost:3000           |
| **Application**             |                        |                                              |
| CUGA (app)                  | ✅ Healthy              | Ready to execute workflows                   |
| Cron                        | ⚠️ UP (no health check) | Running fine, attempting Vault connection    |
| **MCP Services** (10 total) | ✅ All Healthy          | All data connectors ready                    |
| - tradingview-mcp           | ✅ Healthy              | TradingView data on :8001                    |
| - polygon-mcp               | ✅ Healthy              | Polygon.io data on :8002                     |
| - discord-mcp               | ✅ Healthy              | Discord notifications on :8003               |
| - finnhub-mcp               | ✅ Healthy              | Finnhub financial data on :8004              |
| - rot-mcp                   | ✅ Healthy              | ROT sentiment on :8005                       |
| - crypto-orderbook-mcp      | ✅ Healthy              | Crypto orderbook on :8006                    |
| - coingecko-mcp             | ✅ Healthy              | CoinGecko crypto data on :8007               |
| - trading-mcp               | ✅ Healthy              | Trading signals on :8008                     |
| - fred-mcp                  | ✅ Healthy              | Federal Reserve economic data on :8009       |
| - spamshield-mcp            | ✅ Healthy              | Spam filtering on :8010                      |

---

## Vault Initialization (Critical)

### Why This Matters
Vault is your **secret store** for production. It's currently unhealthy because the security barrier hasn't been unsealed. Until it's unsealed, the system can't load secrets for:
- Discord bot tokens
- API keys (Anthropic, Finnhub, FRED, Polygon)
- Database credentials
- Langfuse keys

### Option A: Quick Unsealing (Development)

**Step 1: Check Vault status**
```bash
docker exec trade-alert-vault-1 vault status
```

Expected output shows `Sealed: true`.

**Step 2: Initialize Vault (first time only)**
```bash
docker exec trade-alert-vault-1 vault operator init \
  -key-shares=3 \
  -key-threshold=2
```

This generates:
- **3 unseal keys** (save these securely!)
- **1 initial root token** (save this securely!)

**Example output:**
```
Unseal Key 1: ...
Unseal Key 2: ...
Unseal Key 3: ...
Initial Root Token: hvs.xxxxx
```

💾 **Save these in a secure location** (password manager, encrypted file, etc.)

**Step 3: Unseal Vault**
```bash
# Unseal with Key 1
docker exec trade-alert-vault-1 vault operator unseal <UNSEAL_KEY_1>

# Unseal with Key 2 (threshold is 2, so this completes unsealing)
docker exec trade-alert-vault-1 vault operator unseal <UNSEAL_KEY_2>

# Verify unsealing
docker exec trade-alert-vault-1 vault status
```

Expected: `Sealed: false`

**Step 4: Authenticate with root token**
```bash
docker exec trade-alert-vault-1 vault login <INITIAL_ROOT_TOKEN>
```

**Step 5: Load secrets into Vault**
```bash
# Create secret path
docker exec trade-alert-vault-1 vault kv put secret/trade-alert \
  DISCORD_BOT_TOKEN="your_token_here" \
  ANTHROPIC_API_KEY="your_key_here" \
  FINNHUB_API_KEY="your_key_here" \
  FRED_API_KEY="your_key_here" \
  POLYGON_API_KEY="your_key_here" \
  LANGFUSE_PUBLIC_KEY="your_key_here" \
  LANGFUSE_SECRET_KEY="your_key_here"
```

**Step 6: Set VAULT_TOKEN in `.env`**
```bash
# Update .env.secrets
echo "VAULT_TOKEN=<INITIAL_ROOT_TOKEN>" >> .env.secrets
```

### Option B: Vault-Init Script (Automated, if available)
```bash
# If your project has a vault-init.sh script:
./scripts/vault-init.sh

# This automates: init → unseal → load .env.secrets → output VAULT_TOKEN
```

### Option C: Persistent Unsealing (Production)

For production, use **Shamir key holders** or **cloud unsealing**:
- Distribute unseal keys to team members
- Use HashiCorp Cloud Platform (HCP) Vault for auto-unsealing
- See: https://www.vaultproject.io/docs/concepts/seal

---

## Environment Variables

### Required Variables (MUST Fill)

Secrets go in `.env.secrets` (gitignored). Config tunables go in `.env`.
See `.env.secrets.example` and `.env.example` for templates.

Edit `.env.secrets` (or use Vault):

```bash
# Discord notifications
DISCORD_BOT_TOKEN=xoxb_xxxxx              # From Discord Developer Portal
DISCORD_ALERT_CHANNEL_ID=123456789        # Numeric channel ID
DISCORD_OPS_CHANNEL_ID=123456789          # For operational alerts

# AI/LLM
ANTHROPIC_API_KEY=sk-ant-xxxxx            # From console.anthropic.com

# Market data APIs
FINNHUB_API_KEY=xxxxx                     # From finnhub.io
FRED_API_KEY=xxxxx                        # From stlouisfed.org/fred
POLYGON_API_KEY=xxxxx                     # From polygon.io

# Database (auto-set in docker-compose)
POSTGRES_PASSWORD=<random_secure_password>
POSTGRES_USER=trade_alert

# Langfuse observability (fill after first run — see below)
LANGFUSE_PUBLIC_KEY=xxxxx
LANGFUSE_SECRET_KEY=xxxxx
NEXTAUTH_SECRET=<random_32_char_string>
ENCRYPTION_KEY=<random_32_char_string>
```

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
4. **Add to `.env`:**
   ```bash
   LANGFUSE_PUBLIC_KEY=pk-xxxxx
   LANGFUSE_SECRET_KEY=sk-xxxxx
   ```
5. **Set encryption keys** (generate random strings):
   ```bash
   NEXTAUTH_SECRET=$(openssl rand -hex 16)
   ENCRYPTION_KEY=$(openssl rand -hex 16)
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

# 5. Initialize & unseal Vault
docker exec trade-alert-vault-1 vault operator init -key-shares=3 -key-threshold=2
# → Save unseal keys & root token
docker exec trade-alert-vault-1 vault operator unseal <KEY_1>
docker exec trade-alert-vault-1 vault operator unseal <KEY_2>
docker exec trade-alert-vault-1 vault login <ROOT_TOKEN>
docker exec trade-alert-vault-1 vault kv put secret/trade-alert \
  DISCORD_BOT_TOKEN="..." ANTHROPIC_API_KEY="..." ...

# 6. Update .env with VAULT_TOKEN
echo "VAULT_TOKEN=<ROOT_TOKEN>" >> .env

# 7. Start application layer (CUGA, cron)
docker compose -f docker-compose.prod.yml up -d cuga cron

# 8. Start all MCP services
docker compose -f docker-compose.prod.yml --profile mcp up -d

# 9. Verify all services
docker compose -f docker-compose.prod.yml ps
```

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

# Run with mock data (no real API calls)
docker compose -f docker-compose.prod.yml exec -e MOCK_DATA=1 cuga \
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

**Problem:** `docker exec vault vault status` shows `Sealed: true`

**Solution:**
```bash
# Unseal it
docker exec trade-alert-vault-1 vault operator unseal <UNSEAL_KEY_1>
docker exec trade-alert-vault-1 vault operator unseal <UNSEAL_KEY_2>

# Check status
docker exec trade-alert-vault-1 vault status
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

**Last Updated:** March 11, 2026 | Status: Ready to operate (after Vault init)
