# Copilot Instructions – trade-alert

This repo is a production trading alert engine built on `cuga-agent`.

## SSOT
All architecture, schemas, file names, and implementation rules are defined in:
**`CUGA-Trading-Alert-System-SPEC-v1.3.md`** at the repo root.

## Rules
- Always read `CUGA-Trading-Alert-System-SPEC-v1.3.md` before generating or editing any code.
- Do not deviate from its architecture, file names, schemas, or workflows.
- Do not modify anything under `src/cuga/` — it is a library dependency.
- Generate only the file explicitly requested. Do not auto-refactor other files.
- Secrets are stored in HashiCorp Vault (`secret/trade-alert`, server mode with file backend) and loaded at runtime by `vault_env_loader.py`. Never write keys in code, YAML, or `.env.secrets` is the source file for Vault seeding (git-ignored).
- All Python models must import from `models.py`. No ad-hoc schemas.
- LLM decision agent outputs must be strict JSON matching `PlaybookAlert`.

## Stack Reference
- 20 containers (docker-compose.prod.yml)
- 11 MCP servers (ports 8001–8011): TradingView, Polygon, Discord, Finnhub, ROT, EDGAR, YFinance, Trading, FRED, SpamShield, Alpaca
- 10 signal types: technical_trend, volume_spike, sentiment_bull/bear, options_flow, insider_activity, relative_strength, macro_risk_off, catalyst_event, short_interest
- 6 collectors → merger → Claude Sonnet 4 decision → 7-gate validate_and_filter → notifier
- Redis for snapshot queues (TTL 900s)
- Postgres for alert logging (JSONB) and win-rate history
- Vault (server mode, file backend, auto-unseal)
- Langfuse for prompt management + observability
- Discord bot (discord_bot.py) for ops commands (!scan, !status, !last)
- Discord notifier with mplfinance candlestick chart attachments
