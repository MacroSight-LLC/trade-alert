# Compliance and data-provider posture

trade-alert aggregates market data from third-party APIs. This document describes
operational constraints — it is **not legal advice**.

## Data providers

| Provider | Use | Rate-limit notes |
| -------- | --- | ---------------- |
| Polygon | Quotes, aggregates | Respect plan tier; grouped daily cached |
| Alpaca | Execution bridge (optional) | Paper/live keys via Vault |
| Finnhub | Fundamentals, news | Free tier daily caps |
| FRED | VIX, macro series | Conservative polling |
| TradingView / YFinance | TA and screeners | Best-effort; not execution truth |
| SpamShield | Text classification | Fail-open on MCP errors |

Follow each vendor's Terms of Service. Do not redistribute raw feeds outside
licensed use.

## Regulatory posture

- Alerts are **informational** research outputs, not personalized investment advice.
- No guarantee of accuracy, timeliness, or completeness.
- Operators are responsible for jurisdiction-specific licensing and disclosures.
- Automated execution (when enabled) must use operator-owned brokerage accounts
  with appropriate risk controls.

## Security operations

- Secrets live in HashiCorp Vault (`secret/trade-alert`), never in git.
- Workflow `code:` steps run in `workflow_sandbox.py` — no raw `mcp_call`, `os`, or network imports.
- Execution webhooks require HMAC signatures and idempotency keys (SSOT §11).

## Open follow-ups

Track operational debt in GitHub Issues labeled `tech-debt` (see `FOLLOW_UPS.md` redirect).
