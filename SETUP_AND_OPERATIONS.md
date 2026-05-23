# Trade Alert: Complete Setup & Operations Guide

> Last verified against: v0.10.0 (Phase 10 complete)
> Updated: 2026-05-23

**Status:** 24 containers, persistent Vault (file backend, auto-unseal), 12 MCP data sources, Langfuse observability, Prometheus + Grafana monitoring.

---

## Table of Contents
1. [Current Status](#current-status)
2. [Vault Initialization (Critical)](#vault-initialization-critical)
3. [Environment Variables](#environment-variables)
4. [Full Stack Startup](#full-stack-startup)
5. [Sonnet 4.5 End-to-End Validation (FU-002)](#sonnet-45-end-to-end-validation-fu-002)
6. [Discord Bot Commands](#discord-bot-commands)
7. [Health Checks & Monitoring](#health-checks--monitoring)
8. [Cron Schedule (live)](#cron-schedule-live)
9. [Common Operations](#common-operations)
10. [Remote / VPS Deployment](#remote--vps-deployment)
11. [Troubleshooting](#troubleshooting)

---

## Current Status

### Running Containers (24 total)

| Component                  | Status    | Notes                                           |
| -------------------------- | --------- | ----------------------------------------------- |
| **Infrastructure**         |           |                                                 |
| Vault                      | ✅ Healthy | Server mode, file backend, auto-unseal          |
| Redis                      | ✅ Healthy | Snapshot queues (TTL 900s)                      |
| PostgreSQL (main)          | ✅ Healthy | Alert logging, win-rate history                 |
| PostgreSQL (Langfuse)      | ✅ Healthy | Observability traces (persistent volume)        |
| Langfuse                   | ✅ Healthy | Prompt mgmt + tracing at http://localhost:3000  |
| Prometheus                 | ✅ Healthy | Metrics scraping at http://localhost:9090       |
| Grafana                    | ✅ Healthy | Dashboards at http://localhost:3001             |
| **Application**            |           |                                                 |
| CUGA (app)                 | ✅ Healthy | Pipeline engine                                 |
| Cron                       | ✅ UP      | Scheduled 15m/1h pipeline runs (flock-guarded)  |
| Discord Bot                | ✅ UP      | `!scan`, `!status`, `!last`, `!help`            |
| Dashboard                  | ✅ Healthy | Analytics + Prometheus metrics at :8080         |
| pg-backup                  | ✅ UP      | Daily pg_dump at 03:00 UTC, 7-day retention     |
| **MCP Data Sources** (12)  |           |                                                 |
| tradingview-mcp            | ✅ Healthy | Chart patterns, TA signals on :8001             |
| polygon-mcp                | ✅ Healthy | OHLCV price data, candlestick charts on :8002   |
| discord-mcp                | ✅ Healthy | Discord API proxy on :8003                      |
| finnhub-mcp                | ✅ Healthy | Insider trades, earnings, company data on :8004 |
| rot-mcp                    | ✅ Healthy | Rules-of-thumb signal filtering on :8005        |
| edgar-mcp                  | ✅ Healthy | SEC filings, institutional holdings on :8006    |
| yfinance-mcp               | ✅ Healthy | Options flow, short interest on :8007           |
| trading-mcp                | ✅ Healthy | Position sizing, R:R calculations on :8008      |
| fred-mcp                   | ✅ Healthy | Macro data (VIX, yield curve, CPI) on :8009     |
| spamshield-mcp             | ✅ Healthy | Duplicate/noise filtering on :8010              |
| alpaca-mcp                 | ✅ Healthy | Real-time quotes, market status on :8011        |
| timesfm-mcp                | ✅ Healthy | Time-series forecasting (torch) on :8012        |

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
| `GATE_CONF`                 | `0.70`  | Minimum average confidence for any alert               |
| `UNIVERSE_POLYGON_MIN_CLOSE` | `5.0` | Minimum close price for Polygon volume leaders         |
| `UNIVERSE_POLYGON_MIN_DOLLAR_VOLUME` | `10000000` | Minimum dollar volume for Polygon volume leaders |
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

#### Enable fail-fast Vault enforcement (`VAULT_REQUIRED`)

After `./scripts/vault-init.sh` completes on the production host, add to
`~/trade-alert/.env` (loaded by `docker-compose.prod.yml`):

```bash
VAULT_REQUIRED=true
```

Then restart the stack:

```bash
docker compose -f docker-compose.prod.yml up -d
```

With `VAULT_REQUIRED=true`, `vault_env_loader.py` raises `RuntimeError` at
import time when Vault credentials are missing, authentication fails, the
secret path is empty, or Vault is unreachable after 3 retries. The pipeline
never runs with empty credentials.

**Verify after deploy:**

```bash
# Confirm the flag is set inside the cuga container
docker compose -f docker-compose.prod.yml exec cuga printenv VAULT_REQUIRED

# Confirm secrets were injected (expect a non-zero count)
docker compose -f docker-compose.prod.yml exec cuga python vault_env_loader.py
```

**Negative test (once, manually):** temporarily set an invalid `VAULT_TOKEN` in
`.env.secrets`, restart cuga, and confirm the container fails to start or logs
a `VAULT_REQUIRED=true but ...` error. Restore the correct token afterward.

The deploy workflow smoke test checks that `VAULT_REQUIRED` is truthy inside
the cuga container after every production deploy.

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

### Langfuse Bootstrap (Current Working Path)

Langfuse in this stack is expected to run as a first-class subsystem for:
- prompt management (`decision-system`, `decision-user`)
- trace ingestion and session browsing
- dataset capture (`decision-runs`)
- post-run trace analysis and scoring

The working bootstrap path is:

1. **Put the Langfuse keys and secrets into `.env.secrets`**
  ```bash
  LANGFUSE_PUBLIC_KEY=pk-lf-...
  LANGFUSE_SECRET_KEY=sk-lf-...
  NEXTAUTH_SECRET=$(openssl rand -hex 16)
  ENCRYPTION_KEY=$(openssl rand -hex 32)
  LANGFUSE_INIT_USER_PASSWORD=<strong-password>
  ```
2. **Re-seed Vault** so runtime services receive the same values:
  ```bash
  ./scripts/vault-init.sh
  ```
3. **Start or recreate Langfuse and the app services**:
  ```bash
  set -a && source .env.secrets && set +a
  docker compose -f docker-compose.prod.yml up -d --force-recreate langfuse cuga cron discord-bot
  ```
4. **Seed production prompts**:
  ```bash
  docker compose -f docker-compose.prod.yml exec -T cuga \
    python scripts/seed_langfuse_prompts.py
  ```
5. **Run the doctor script** from the CUGA runtime context:
  ```bash
  docker compose -f docker-compose.prod.yml exec -T cuga \
    python scripts/langfuse_doctor.py
  ```

The production deploy script runs this doctor check automatically after prompt
seeding and before cron/automation services are started, so prompt/runtime
readiness is validated before scheduled workflows begin.

Expected result:
- transport `OK`
- auth `OK`
- `decision-system` and `decision-user` prompts available under the `production` label
- `decision-runs` dataset visible or created on first successful decision cycle
- recent traces visible for at least the active timeframe

### Langfuse Bootstrap Repair (If `/health` is Green but Runtime Auth Fails)

Symptom pattern:
- `http://localhost:3000/api/public/health` returns 200
- but CUGA gets 401 / `Invalid credentials. Confirm that you've configured the correct host.`

This means Langfuse is reachable, but its project/API-key rows do not match the
runtime `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` values.

Run this sequence:

1. **Check doctor output**
  ```bash
  docker compose -f docker-compose.prod.yml exec -T cuga \
    python scripts/langfuse_doctor.py --strict
  ```
2. **Re-seed Vault and recreate services**
  ```bash
  ./scripts/vault-init.sh
  set -a && source .env.secrets && set +a
  docker compose -f docker-compose.prod.yml up -d --force-recreate langfuse cuga cron discord-bot
  ```
3. **Re-seed prompts**
  ```bash
  docker compose -f docker-compose.prod.yml exec -T cuga \
    python scripts/seed_langfuse_prompts.py
  ```
4. **Run doctor again**
  ```bash
  docker compose -f docker-compose.prod.yml exec -T cuga \
    python scripts/langfuse_doctor.py --strict
  ```

If runtime auth still fails after this, Langfuse DB bootstrap is incomplete and
must be repaired before prompt management, trace analysis, and dataset capture
will work correctly.

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

# 3. Start the full stack (all 24 containers)
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

### Langfuse Verification Commands

Quick post-deploy verification:

```bash
docker compose -f docker-compose.prod.yml exec -T cuga \
  python scripts/langfuse_doctor.py
```

Automated path:

```bash
./scripts/deploy.sh
```

This now seeds prompts, runs the Langfuse doctor automatically, and only then
starts cron / automation services.

Machine-readable output:

```bash
docker compose -f docker-compose.prod.yml exec -T cuga \
  python scripts/langfuse_doctor.py --json
```

Strict mode for CI / deploy checks:

```bash
docker compose -f docker-compose.prod.yml exec -T cuga \
  python scripts/langfuse_doctor.py --strict
```

Live end-to-end proof runs:

```bash
docker compose -f docker-compose.prod.yml exec -T cuga \
  python pipeline_runner.py workflows/orchestrator-15m.yaml

docker compose -f docker-compose.prod.yml exec -T cuga \
  python pipeline_runner.py workflows/orchestrator-1h.yaml
```

After a successful run, confirm recent traces directly:

```bash
docker compose -f docker-compose.prod.yml exec -T cuga sh -lc 'python - <<"PY"
from langfuse_client import reset_client, get_langfuse_client
reset_client()
lf = get_langfuse_client()
for tf in ("15m", "1h"):
    traces = lf.fetch_traces(session_id=f"orchestrator-{tf}", limit=1, order_by="timestamp.DESC")
    latest = traces.data[0].id if getattr(traces, "data", None) else "none"
    print(tf, latest)
PY'
```

### Intermittent Langfuse Internal Errors

You may still see intermittent messages like:

```text
Internal error occurred. This is an unusual occurrence and we are monitoring it closely.
```

Observed behavior in this repo:
- prompts can still load successfully from Langfuse
- dataset capture can still succeed
- trace scoring/finalization can still succeed
- Discord alert delivery is not blocked

Treat this as **degraded but non-blocking** unless one of these starts failing:
- prompt fetch returns 401/404 unexpectedly
- doctor reports `auth=fail`
- traces stop appearing for active sessions
- datasets stop receiving new items

Use the doctor script first before taking any reset action.

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

## Sonnet 4.5 End-to-End Validation (FU-002)

The codebase uses `claude-sonnet-4-5` across all decision workflows. After any
model migration, run at least one full 15m and 1h pipeline cycle in a live
environment and confirm output quality before treating the switch as validated.

### Trigger pipeline cycles

```bash
DC="docker compose -f docker-compose.prod.yml"

# 15m orchestrator (collectors → merger → decision → notifier)
$DC exec cuga python pipeline_runner.py workflows/orchestrator-15m.yaml

# 1h orchestrator
$DC exec cuga python pipeline_runner.py workflows/orchestrator-1h.yaml
```

Alternatively, wait for cron or use the Discord bot: `!scan 15m` / `!scan 1h`.

### Capture evidence

1. **Langfuse traces** — Open Langfuse UI and confirm the decision step shows
   model `claude-sonnet-4-5`. Note trace IDs from the pipeline-summary JSON
   logged at the end of each orchestrator run.

2. **PlaybookAlert schema** — Decision output must parse cleanly:
   ```bash
   $DC exec cuga python -c "
   import json
   from models import PlaybookAlert
   # Replace with actual decision output path from workflow logs
   alerts = json.loads(open('/app/logs/decision-15m-output.json').read())
   for a in alerts:
       PlaybookAlert.model_validate(a)
   print(f'Validated {len(alerts)} alert(s)')
   "
   ```

3. **Gate-rejection mix** — Compare Prometheus `gate_rejections_total` breakdown
   against the historical envelope (±15% per gate vs the 7-day median before
   migration):
   ```bash
   curl -s http://localhost:9090/api/v1/query?query=gate_rejections_total
   # Or use the dashboard: http://localhost:8080/api/summary
   ```

4. **Notifier stage** — Confirm the pipeline reaches Discord MCP even if zero
   alerts pass gates (check notifier step in Langfuse trace or pipeline logs).

### Acceptance envelope

| Criterion | Pass condition |
| --------- | -------------- |
| Schema | Zero `PlaybookAlert` Pydantic validation failures in decision/notifier stages |
| Gate mix | Per-gate rejection counts within ±15% of 7-day pre-migration median |
| Model ID | Langfuse traces show `claude-sonnet-4-5` |
| Pipeline | Both 15m and 1h orchestrators complete with pipeline-summary JSON |
| Alerts | At least one alert through full pipeline **or** explicit zero-alert run during quiet market (document reason) |

### Automated helper

Run the checklist script on the production host:

```bash
./deployment/validate-sonnet-4-5.sh
```

Record trace IDs and gate-rejection snapshot in [`FOLLOW_UPS.md`](./FOLLOW_UPS.md)
when validation passes, then move FU-002 to Resolved.

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

# Prometheus targets
curl http://localhost:9090/api/v1/targets
```

### Prometheus & Grafana

```bash
# Prometheus UI → http://localhost:9090
# Grafana UI   → http://localhost:3001 (admin/admin)

# Exposed metrics (scraped from dashboard :8080/metrics):
#   pipeline_run_total          — counter of pipeline executions by timeframe
#   pipeline_run_active         — gauge of currently running pipelines
#   mcp_call_duration_seconds   — histogram of MCP call latency by endpoint
#   circuit_breaker_trip_total  — counter of MCP circuit breaker trips
#   gate_rejection_total        — counter of validation gate rejections by gate name
#   alerts_per_cycle            — histogram of alerts fired per pipeline run
#   discord_send_total          — counter of Discord sends (success/fail)
#   db_insert_total             — counter of alert DB inserts
#   chart_gen_duration_seconds  — histogram of candlestick chart generation time
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

## Cron Schedule (live)

SSOT §5 describes the cron schedule generically ("every 15 min", "every
hour"). The actual `crontab` shipped with the cron container is more
defensive — it only runs orchestrators during US market hours so the
pipeline never hits MCPs when the markets are closed. This section
documents the live schedule for operations reference. **All times are
ET (`TZ=America/New_York`, DST-aware).**

| Job                       | Schedule (ET)                    | Notes                                                      |
|---------------------------|----------------------------------|------------------------------------------------------------|
| `orchestrator-15m.yaml`   | 9:30, 9:45, then `*/15` 10–15h, and 16:00 Mon–Fri | Open + close window guarded by `flock`                     |
| `orchestrator-1h.yaml`    | `0 10–16 * * 1–5`                | One run at the top of each hour during the trading day      |
| `outcome-tracker.yaml`    | `*/15 9–18 * * 1–5`              | Extended window so positions opened late still resolve     |
| `state-summary.yaml`      | 9:15 and 16:15 Mon–Fri           | Pre-open prep + post-close digest                          |
| `eod_summary.py`          | 16:15 Mon–Fri                    | Discord ops EOD recap                                      |
| `healthcheck.py`          | every 15 min, 24/7               | Independent of market hours so infra rot is detected fast  |
| Log rotation              | midnight daily                   | Tail-truncates `health.jsonl` and `cron.log` to last 50k   |

The SSOT remains intentionally generic; this table is the single place
that catalogs the live schedule.  When updating `crontab`, update this
table in the same commit.

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

### CI deploy (GitHub Actions → Hetzner)

Every push to `main` runs [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml):

1. Lint and unit-test the commit (locked `uv` deps, `pgvector/pgvector:pg16` Postgres).
2. Build and push four images to GHCR tagged with the commit SHA: `cuga`, `mcp`, `timesfm`, `dashboard`.
3. **If** repository variable `HETZNER_PROVISIONED=true`: SSH to the VPS, `git checkout` the exact SHA, `docker login ghcr.io`, pull pre-built images, and `docker compose up --no-build`, then run post-deploy smoke checks.

**Hetzner is not provisioned yet.** Deploy and smoke are gated off by default (`HETZNER_PROVISIONED` unset or not `true`). Lint, test, and GHCR build still run so images stay current. See [`RELEASING.md`](RELEASING.md) § Hetzner deploy target (planned).

### Hetzner provisioning checklist

Complete before setting `HETZNER_PROVISIONED=true`:

- [ ] **Server** — Hetzner VPS created (Ubuntu LTS recommended); firewall allows SSH from GitHub Actions egress (or use a self-hosted runner later).
- [ ] **Deploy user** — `deploy` user with sudo-less Docker access; SSH key pair generated for CI.
- [ ] **Docker** — Docker Engine + Compose plugin installed (`scripts/bootstrap_vps.sh` or equivalent).
- [ ] **Repository clone** — `~/trade-alert` on the VPS, `git remote` pointing at `MacroSight-LLC/trade-alert`.
- [ ] **Secrets file** — `.env.secrets` populated on the host (see `.env.secrets.example`); Vault initialized (`scripts/vault-init.sh`).
- [ ] **GHCR login on VPS** — verify manually: `echo "$TOKEN" | docker login ghcr.io -u USER --password-stdin` then `docker pull ghcr.io/MacroSight-LLC/trade-alert/cuga:latest`.
- [ ] **GitHub Actions secrets** — set in repo Settings → Secrets and variables → Actions:
  - `HETZNER_HOST` — VPS IP or hostname
  - `HETZNER_SSH_KEY` — private key for `deploy` user
  - `GHCR_READ_TOKEN` — fine-grained PAT, **Packages: Read** on `MacroSight-LLC/trade-alert`
- [ ] **Enable CI deploy** — `gh variable set HETZNER_PROVISIONED --body true --repo MacroSight-LLC/trade-alert`
- [ ] **First deploy** — push to `main` or re-run **Deploy trade-alert** workflow; confirm smoke passes (MCP ports 8001–8012).

Required GitHub Actions secrets and variables: see [`RELEASING.md`](RELEASING.md).

Image overrides in [`docker-compose.prod.yml`](docker-compose.prod.yml) (defaults point at GHCR `latest`; CI sets SHA tags):

```bash
export CUGA_IMAGE=ghcr.io/MacroSight-LLC/trade-alert/cuga:<sha>
export MCP_IMAGE=ghcr.io/MacroSight-LLC/trade-alert/mcp:<sha>
export TIMESFM_IMAGE=ghcr.io/MacroSight-LLC/trade-alert/timesfm:<sha>
export DASHBOARD_IMAGE=ghcr.io/MacroSight-LLC/trade-alert/dashboard:<sha>
docker compose -f docker-compose.prod.yml pull cuga cron discord-bot dashboard timesfm-mcp tradingview-mcp
docker compose -f docker-compose.prod.yml up -d --no-build
docker compose -f docker-compose.prod.yml --profile mcp up -d --no-build
```

Production `cuga` bind-mounts Python sources from the git checkout, so the deploy pins **both** git SHA and image SHA.

### Manual deploy (local build)

For first-time setup or when GHCR is unavailable, use [`scripts/deploy.sh`](scripts/deploy.sh) or:

```bash
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

### 1. Port Security (Critical)

By default, `docker-compose.prod.yml` binds internal services to **127.0.0.1**
(loopback only). This includes Redis, Postgres, Vault, Langfuse-DB, and **MCP servers
(8001-8012)** — they are NOT publicly accessible. Only containers on the
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
| Grafana     | 3001          | `grafana.yourdomain.com`    |
| MCP servers | 8001-8012     | Not proxied (loopback only) |

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

**Last Updated:** April 2026 | Status: 24 containers, persistent Vault (file backend), 12 MCPs, 11 signal types, Prometheus + Grafana monitoring
