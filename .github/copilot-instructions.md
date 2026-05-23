# Copilot Instructions – trade-alert

This repo is a production trading alert engine built on `cuga-agent`.

## SSOT
All architecture, schemas, file names, and implementation rules are defined in:
**`docs/spec-v1.3.md`** (symlinked as `SSOT.md` at repo root).

## Workflow authoring
- Workflow `code:` steps run in **`workflow_sandbox.py`** — blocked: `mcp_call`, `os`, `redis`, `httpx`, dunder names.
- Use `type: tool_call` or `parallel_tool_calls` for MCP — never inline `mcp_call()` in `code:` blocks.
- Redis snapshot TTL is **1200s** (20 min) per SSOT §8.
- Example tool step:
  ```yaml
  - name: fetch-vix
    type: tool_call
    tool: fred-mcp
    method: vix_level
    params: {}
  ```

## Rules
- Always read `docs/spec-v1.3.md` before generating or editing any code.
- Do not deviate from its architecture, file names, schemas, or workflows.
- Do not modify anything under `src/cuga/` — it is a library dependency.
- Generate only the file explicitly requested. Do not auto-refactor other files.
- Secrets are stored in HashiCorp Vault (`secret/trade-alert`, server mode with file backend) and loaded at runtime by `vault_env_loader.py`. Never write keys in code, YAML, or `.env.secrets` is the source file for Vault seeding (git-ignored).
- All Python models must import from `models.py`. No ad-hoc schemas.
- LLM decision agent outputs must be strict JSON matching `PlaybookAlert`.

## Stack Reference
- 24 containers (docker-compose.prod.yml)
- 12 MCP servers (ports 8001–8012): TradingView, Polygon, Discord, Finnhub, ROT, EDGAR, YFinance, Trading, FRED, SpamShield, Alpaca, TimesFM
- 11 signal types: technical_trend, volume_spike, sentiment_bull/bear, options_flow, insider_activity, relative_strength, macro_risk_off, catalyst_event, short_interest, price_forecast
- 7 collectors → merger → claude-sonnet-4-5 decision → 23-gate validate_and_filter → notifier
- Gate inventory: SSOT §10.4 + `GateRejection` enum in `validate_and_filter.py`
- Redis for snapshot queues (TTL 900s)
- Postgres for alert logging (JSONB) and win-rate history
- Vault (server mode, file backend, auto-unseal)
- Langfuse for prompt management + observability
- Prometheus + Grafana for metrics and dashboards
- Discord bot (discord_bot.py) for ops commands (!scan, !status, !last)
- Discord notifier with mplfinance candlestick chart attachments
