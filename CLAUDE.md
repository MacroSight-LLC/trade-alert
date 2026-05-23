# CLAUDE.md – Project Context for Claude

This is the `/trade-alert` project: a production trading alert engine built on `cuga-agent`.

## SSOT
The full architecture, schemas, and implementation rules are in:
**`CUGA-Trading-Alert-System-SPEC-v1.3.md`** at the repo root.

## Rules
- Always read and follow `CUGA-Trading-Alert-System-SPEC-v1.3.md` before generating or editing any code.
- Do not deviate from its architecture, file names, or schemas.
- Do not modify files under `src/cuga/` — treat them as a library.
- No secrets in code or YAML. Secrets are stored in HashiCorp Vault (`secret/trade-alert`, server mode with file backend) and loaded at runtime by `vault_env_loader.py`. The `.env.secrets` file holds secret values for Vault seeding (git-ignored).
- Generate one file at a time, scoped to the section referenced.

## AI-Development Guardrails (SSOT §0.2)

Use `CUGA-Trading-Alert-System-SPEC-v1.3.md` as the single source of truth.
Do not add new concepts or deviate from its architecture, schemas, or filenames.

When generating or editing a file:

1. Name the target file explicitly.
2. Reference the relevant section of this spec.
3. For workflows, follow the CUGA YAML patterns from the official `cuga-agent`
   examples but with the tools and prompts from this spec.

Never let AI tools auto-refactor across the whole repo. Limit them to the file
or function specified.

## Key files

| File | Purpose |
| ---- | ------- |
| [`CUGA-Trading-Alert-System-SPEC-v1.3.md`](./CUGA-Trading-Alert-System-SPEC-v1.3.md) | Authoritative spec (SSOT). `SSOT.md` is a symlink to this file. |
| [`README.md`](./README.md) | Quick-start overview and developer entry point. |
| [`README.cuga.md`](./README.cuga.md) | Full CUGA framework reference (upstream docs). |
| [`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md) | Deployment and operations runbook. |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | Contribution rules, commit conventions, secrets baseline policy. |
| [`CHANGELOG.md`](./CHANGELOG.md) | Per-phase release history (Keep a Changelog format). |

## Stack Reference
- 24 containers (docker-compose.prod.yml)
- 12 MCP servers (ports 8001–8012): TradingView, Polygon, Discord, Finnhub, ROT, EDGAR, YFinance, Trading, FRED, SpamShield, Alpaca, TimesFM
- 11 signal types: technical_trend, volume_spike, sentiment_bull, sentiment_bear, options_flow, insider_activity, relative_strength, macro_risk_off, catalyst_event, short_interest, price_forecast
- 7 collectors, merger, Claude Sonnet 4.5 decision engine, 23-gate validate_and_filter, notifier with candlestick charts
- Redis for snapshot queues; WATCH decay uses `watch:decay:{timeframe}:{symbol}` keys
- Gate-level dedup keys: `dedup:alert:{timeframe}:{direction}:{symbol}`
- Redis circuit breaker env vars: `REDIS_FAILURE_THRESHOLD`, `REDIS_FAILURE_WINDOW_SECONDS`
- Dedup env vars: `ALERT_DEDUP_TTL_SECONDS`, `ALERT_DEDUP_ENABLED`, `WATCH_DEDUP_TTL_SECONDS`
- Execution bridge idempotency via `idempotency_key` column and `ExecutionPayload` schema v1.0
- Notifier modules: `discord_formatter.py`, `alert_logger.py`, `notifier.py` (shim: `notifier_and_logger.py`)
- Prometheus: `trade_alert_redis_circuit_open`, `trade_alert_watch_decay_skipped_total`
- Postgres for alert logging (JSONB) and win-rate history
- Vault (server mode, file backend, auto-unseal via deployment/vault-entrypoint.sh)
- Langfuse for prompt management + observability tracing
- Prometheus + Grafana for metrics and dashboards
- Discord bot (discord_bot.py) for !scan, !status, !last commands
- CUGA YAML workflows (collectors + decisions + orchestrators)
