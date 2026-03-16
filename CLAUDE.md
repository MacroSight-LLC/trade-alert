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

## Stack Reference
- 20 containers (docker-compose.prod.yml)
- 11 MCP servers (ports 8001–8011): TradingView, Polygon, Discord, Finnhub, ROT, EDGAR, YFinance, Trading, FRED, SpamShield, Alpaca
- 10 signal types: technical_trend, volume_spike, sentiment_bull, sentiment_bear, options_flow, insider_activity, relative_strength, macro_risk_off, catalyst_event, short_interest
- 6 collectors, merger, Claude Sonnet 4 decision engine, 7-gate validate_and_filter, notifier with candlestick charts
- Redis for snapshot queues (TTL 900s)
- Postgres for alert logging (JSONB) and win-rate history
- Vault (server mode, file backend, auto-unseal via deployment/vault-entrypoint.sh)
- Langfuse for prompt management + observability tracing
- Discord bot (discord_bot.py) for !scan, !status, !last commands
- CUGA YAML workflows (collectors + decisions + orchestrators)
