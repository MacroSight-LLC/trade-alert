# Trade Alert: Complete Setup & Operations Guide

**Status:** 20 containers, persistent Vault (file backend, auto-unseal), 11 MCP data sources, Langfuse observability.

---

## Table of Contents
1. [Current Status](#current-status)
2. [Vault Initialization (Critical)](#vault-initialization-critical)
3. [Environment Variables](#environment-variables)
4. [Full Stack Startup](#full-stack-startup)
5. [Discord Bot Commands](#discord-bot-commands)
6. [Health Checks & Monitoring](#health-checks--monitoring)
7. [Common Operations](#common-operations)
8. [Remote / VPS Deployment](#remote--vps-deployment)
9. [Troubleshooting](#troubleshooting)

---

## Current Status

### Running Containers (20 total)

| Component                 | Status    | Notes                                           |
| ------------------------- | --------- | ----------------------------------------------- |
| **Infrastructure**        |           |                                                 |
| Vault                     | ✅ Healthy | Server mode, file backend, auto-unseal          |
| Redis                     | ✅ Healthy | Snapshot queues (TTL 900s)                      |
| PostgreSQL (main)         | ✅ Healthy | Alert logging, win-rate history                 |
| PostgreSQL (Langfuse)     | ✅ Healthy | Observability traces (persistent volume)        |
| Langfuse                  | ✅ Healthy | Prompt mgmt + tracing at http://localhost:3000  |
| **Application**           |           |                                                 |
| CUGA (app)                | ✅ Healthy | Pipeline engine                                 |
| Cron                      | ✅ UP      | Scheduled 15m/1h pipeline runs                  |
| Discord Bot               | ✅ UP      | `!scan`, `!status`, `!last`, `!help`            |
| Dashboard                 | ✅ Healthy | Analytics at http://localhost:8080              |
| **MCP Data Sources** (11) |           |                                                 |
| tradingview-mcp           | ✅ Healthy | Chart patterns, TA signals on :8001             |
| polygon-mcp               | ✅ Healthy | OHLCV price data, candlestick charts on :8002   |
| discord-mcp               | ✅ Healthy | Discord API proxy on :8003                      |
| finnhub-mcp               | ✅ Healthy | Insider trades, earnings, company data on :8004 |
| rot-mcp                   | ✅ Healthy | Rules-of-thumb signal filtering on :8005        |
| edgar-mcp                 | ✅ Healthy | SEC filings, institutional holdings on :8006    |
| yfinance-mcp              | ✅ Healthy | Options flow, short interest on :8007           |
| trading-mcp               | ✅ Healthy | Position sizing, R:R calculations on :8008      |
| fred-mcp                  | ✅ Healthy | Macro data (VIX, yield curve, CPI) on :8009     |
| spamshield-mcp            | ✅ Healthy | Duplicate/noise filtering on :8010              |
| alpaca-mcp                | ✅ Healthy | Real-time quotes, market status on :8011        |

---

## Pipeline Tunables (Non-Secret Environment Variables)

These `.env` variables control pipeline behavior and can be overridden without touching code:

| Variable                    | Default | Description                                            |
| --------------------------- | ------- | ------------------------------------------------------ |
| `VIX_EXTREME_THRESHOLD`     | `35.0`  | VIX above this → `macro_risk_off` score 3.0            |
| `VIX_ELEVATED_THRESHOLD`    | `25.0`  | VIX above this → `macro_risk_off` score 2.0            |
| `CURVE_INVERSION_THRESHOLD` | `-50.0` | Yield curve below this (bps) → inversion signal        |
| `GATE_EP_15M`               | `0.70`  | Minimum edge_probability for 15m alerts                |
| `GATE_EP_1H`                | `0.75`  | Minimum edge_probability for 1h alerts                 |
| `GATE_SA`                   | `3`     | Minimum sources_agree for any alert                    |
| `GATE_CONF`                 | `0.75`  | Minimum average confidence for any alert               |
| `MERGER_TOP_N`              | `20`    | Max symbols passed to the decision engine              |
| `REDIS_SNAPSHOT_TTL`        | `1200`  | TTL (seconds) for Redis snapshot queues                |
| `OUTCOME_WINDOW_HOURS`      | `4`     | Default alert expiry window (fallback if no timeframe) |

---

## Vault Initialization (Critical)

### Why This Matters
Vault is your **secret store** for production. All API keys, tokens, and
credentials live exclusively in Vault at `secret/trade-alert`. The `.env.secrets`
file is the source for seeding; secrets are loaded at runtime by `vault_env_loader.py`.

### How It Works

Vault runs in **server mode** with a **file backend** (`deployment/vault-config.hcl`).
Secrets persist across container restarts via the `vault-data` Docker volume.
An auto-unseal entrypoint (`deployment/vault-entrypoint.sh`) reads the unseal key
from `.vault-init.json` and unseals automatically on boot.

```bash
# First-time setup (initializes Vault, generates unseal keys, seeds secrets):
./scripts/vault-init.sh

# The script will:
# 1. Wait for Vault to be listening
# 2. Initialize Vault (if not already initialized) — saves keys to .vault-init.json
# 3. Unseal Vault
# 4. Enable KV v2 secrets engine
# 5. Read .env.secrets and write all secrets to Vault
# 6. Generate .env.vault for MCP containers
```

**Important files:**
- `.vault-init.json` — unseal keys + root token (git-ignored, DO NOT commit)
- `.env.secrets` — source of truth for secret values (git-ignored)
- `deployment/vault-config.hcl` — Vault server configuration (file backend)
- `deployment/vault-entrypoint.sh` — auto-unseal on container start

### After Volume Loss (e.g., `docker compose down -v`)

All secrets are lost. Re-initialize:
```bash
rm .vault-init.json   # remove stale init file
docker compose -f docker-compose.prod.yml up -d vault
./scripts/vault-init.sh   # re-initializes and re-seeds
# Update VAULT_TOKEN in .env.secrets with the new token printed by the script
```

### Production Considerations

For production, replace the file backend with cloud auto-unseal:
- Use **AWS KMS**, **GCP CKMS**, or **Azure Key Vault** for auto-unseal
- Generate a scoped AppRole or token (not root) and set `VAULT_TOKEN` in the
  deployment environment
- See: https://www.vaultproject.io/docs/concepts/seal

### Vault Secrets Stored

After running `vault-init.sh`, verify with:

```bash
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=<your-root-token>  # from .vault-init.json or .env.secrets
vault kv get -format=json secret/trade-alert | python3 -c "import json,sys; [print(f'  {k}') for k in sorted(json.load(sys.stdin)['data']['data'].keys())]"
```

Expected keys (17):
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY`,
`DISCORD_ALERT_CHANNEL_ID`, `DISCORD_BOT_TOKEN`, `DISCORD_OPS_CHANNEL_ID`,
`EDGAR_USER_AGENT`, `ENCRYPTION_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`,
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
| Alpaca            | https://app.alpaca.markets/paper/dashboard/overview     | 5 min |

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

# 1. Create .env.secrets with your API keys
cp .env.example .env.secrets
# ← EDIT .env.secrets with your API keys ←

# 2. Load secrets into shell environment
set -a && source .env.secrets && set +a

# 3. Start the full stack (all 20 containers)
docker compose -f docker-compose.prod.yml --profile mcp up -d

# 4. Wait for PostgreSQL to initialize (30-60 seconds)
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U trade_alert

# 5. Apply database schema
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U trade_alert -d trade_alert -f /docker-entrypoint-initdb.d/schema.sql

# 6. Initialize Vault and seed secrets
./scripts/vault-init.sh
# ⚠️ If this is the first run, update .env.secrets with the new VAULT_TOKEN printed by the script

# 7. Seed Langfuse prompts (enables live editing via UI)
docker compose -f docker-compose.prod.yml exec cuga \
  python scripts/seed_langfuse_prompts.py

# 8. Verify all services
docker compose -f docker-compose.prod.yml --profile mcp ps
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
set -a && source .env.secrets && set +a
docker compose -f docker-compose.prod.yml --profile mcp up -d
```

### Shutdown

```bash
docker compose -f docker-compose.prod.yml --profile mcp down
```

### Shutdown + Cleanup Data

```bash
docker compose -f docker-compose.prod.yml --profile mcp down -v
# This deletes: Redis data, Vault data + secrets, Langfuse database, Postgres alerts
# You will need to re-run vault-init.sh and seed_langfuse_prompts.py after restart
```

---

## Discord Bot Commands

The Discord bot runs in the `discord-bot` container, polling the **ops channel** for commands:

| Command     | Description                                             |
| ----------- | ------------------------------------------------------- |
| `!scan`     | Run the 15m pipeline now                                |
| `!scan 1h`  | Run the 1h pipeline now                                 |
| `!scan 15m` | Run the 15m pipeline (explicit)                         |
| `!status`   | Show pipeline health, Redis snapshot counts, MCP status |
| `!last`     | Show the most recent fired alert from Postgres          |
| `!help`     | Show available commands                                 |

**Channel routing:**
- **Ops channel** (`DISCORD_OPS_CHANNEL_ID`): Bot listens here for commands
- **Alert channel** (`DISCORD_ALERT_CHANNEL_ID`): Alerts + candlestick charts are posted here by the notifier

**Concurrency:** Only one `!scan` can run at a time; subsequent requests are queued.

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

## Remote / VPS Deployment

When moving from localhost to a remote host (VPS, cloud VM, dedicated server),
the codebase is **already parameterized** — you only need to change environment
variables, not code. Here's what to configure:

### 1. Port Security (Critical)

By default, `docker-compose.prod.yml` binds internal services to **127.0.0.1**
(loopback only). This includes Redis, Postgres, Vault, Langfuse-DB, and **MCP servers
(8001-8011)** — they are NOT publicly accessible. Only containers on the
same Docker network can reach them.

Langfuse UI (:3000) and Dashboard (:8080) default to `0.0.0.0` for browser access.
Protect them with a reverse proxy (nginx/caddy) + TLS in production.

If you need remote access to a specific service (e.g., for external monitoring),
override in `.env`:

```bash
# Only do this if you have a firewall / security group blocking external access:
POSTGRES_BIND=0.0.0.0    # Expose Postgres externally (DANGEROUS without firewall)
MCP_BIND=0.0.0.0         # Expose MCP servers externally (NOT recommended)

# Restrict Langfuse/Dashboard to loopback (if behind a reverse proxy on the same host):
LANGFUSE_BIND=127.0.0.1
DASHBOARD_BIND=127.0.0.1
```

### 2. Langfuse Configuration

**Self-hosted Langfuse on the same host (recommended):**

```bash
# .env — set NEXTAUTH_URL to your server's public URL:
NEXTAUTH_URL=https://langfuse.yourdomain.com

# docker-compose already sets LANGFUSE_HOST=http://langfuse:3000 for
# inter-container communication. No change needed.
```

**Langfuse Cloud (managed):**

```bash
# .env.secrets — use Langfuse Cloud credentials:
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx

# .env — point to cloud:
# (Not needed in docker-compose; override for the seed script)
LANGFUSE_HOST=https://cloud.langfuse.com
```

When using Langfuse Cloud, you can remove the `langfuse` and `langfuse-db`
services from docker-compose.prod.yml entirely.

### 3. Seeding Prompts on a Remote Host

```bash
# From inside the container (self-hosted, inter-container DNS):
docker compose -f docker-compose.prod.yml exec cuga \
  python scripts/seed_langfuse_prompts.py

# From the host machine (needs LANGFUSE_HOST set):
LANGFUSE_HOST=http://localhost:3000 python scripts/seed_langfuse_prompts.py

# Langfuse Cloud (from anywhere with credentials):
python scripts/seed_langfuse_prompts.py --host https://cloud.langfuse.com

# Update existing prompts (creates new version):
python scripts/seed_langfuse_prompts.py --update
```

### 4. Reverse Proxy (Recommended)

For production, place nginx or caddy in front of public-facing services:

| Service     | Internal Port | Public Path                 |
| ----------- | ------------- | --------------------------- |
| Langfuse UI | 3000          | `langfuse.yourdomain.com`   |
| Dashboard   | 8080          | `dash.yourdomain.com`       |
| MCP servers | 8001-8011     | Not proxied (loopback only) |

MCP servers are bound to `127.0.0.1` by default and should **never** be
exposed publicly — they're called only by the cuga container over the Docker
network.

### 5. Vault on a Remote Host

Vault configuration doesn't change — it uses the Docker network internally.
Ensure `.vault-init.json` is securely transferred to the remote host and
**never** committed to version control.

```bash
# On the remote host:
scp .vault-init.json user@remote:/path/to/trade-alert/
scp .env.secrets user@remote:/path/to/trade-alert/
```

### 6. TLS Considerations

- **Langfuse:** Set `NEXTAUTH_URL` to `https://...` and terminate TLS at the proxy.
- **Vault:** For production, enable TLS in `deployment/vault-config.hcl` and use
  `VAULT_ADDR=https://vault:8200`. Place certs in `deployment/certs/`.
- **Postgres:** Use `sslmode=require` in `DATABASE_URL` if Postgres is remote.

### 7. Langfuse API — No Code Changes Needed

The Langfuse Python SDK communicates via HTTP REST API. The connection is
fully parameterized through environment variables:

- `LANGFUSE_HOST` — SDK endpoint (default: `http://localhost:3000`)
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` — authentication

Inside Docker Compose, these are set to internal DNS names. When running
outside Docker or against Langfuse Cloud, just override the env vars.
The SDK, prompt management, tracing, and dataset capture all work identically
regardless of whether Langfuse is local, remote, or cloud-hosted.

---

## Troubleshooting

### Vault Stays Unhealthy

**Problem:** `docker exec trade-alert-vault-1 vault status` returns sealed or error

**Solution:** Vault uses server mode with auto-unseal. If `.vault-init.json` is
present and mounted, the entrypoint script unseals automatically. If it fails:
```bash
# Manual unseal
export VAULT_ADDR=http://127.0.0.1:8200
UNSEAL_KEY=$(python3 -c "import json; print(json.load(open('.vault-init.json'))['unseal_keys_b64'][0])")
vault operator unseal "$UNSEAL_KEY"

# Re-seed if needed
export VAULT_TOKEN=$(python3 -c "import json; print(json.load(open('.vault-init.json'))['root_token'])")
./scripts/vault-init.sh
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

- **Architecture:** [`CUGA-Trading-Alert-System-SPEC-v1.3.md`](./CUGA-Trading-Alert-System-SPEC-v1.3.md)
- **Application docs:** [`README.cuga.md`](./README.cuga.md)
- **Vault docs:** https://www.vaultproject.io/docs
- **Docker Compose:** https://docs.docker.com/compose/
- **Langfuse:** https://langfuse.com/docs

---

**Last Updated:** March 2026 | Status: 20 containers, persistent Vault (file backend), 11 MCPs, 10 signal types
