# Static dashboard — www.macrosight.net/trade-alert/

Deploy **`index.html`** to your static site at:

`https://www.macrosight.net/trade-alert/`

## Regenerate from source

```bash
# Default API host: https://trade-alert-api.macrosight.net
./scripts/sync_static_dashboard.sh

# Or override:
TRADE_ALERT_API_BASE=https://your-vps.example.com ./scripts/sync_static_dashboard.sh
```

Upload the generated `index.html` to your web host (S3, Netlify, nginx docroot, etc.).

## Backend requirements

The page calls JSON APIs on the trade-alert **dashboard** service (`dashboard_api.py`):

| Endpoint | Purpose |
|----------|---------|
| `/api/summary` | KPI header |
| `/api/kpis` | Gate rejection rate, poll interval |
| `/api/health` | Redis / system status |
| `/api/alerts` | Recent alerts table |
| `/api/winrate`, `/api/frequency`, `/api/symbols`, `/api/session-stats` | Charts |

On the VPS, set:

```bash
DASHBOARD_CORS_ORIGINS=https://www.macrosight.net
DASHBOARD_API_KEY=<strong-secret>
DASHBOARD_REQUIRE_AUTH=true
```

Expose the dashboard container via HTTPS (e.g. `trade-alert-api.macrosight.net` → `:8080`). See [docs/DASHBOARD_STATIC_HOSTING.md](../../docs/DASHBOARD_STATIC_HOSTING.md).

## First visit

Browsers will prompt for the **X-API-Key** (stored in `localStorage`). Override API URL once with:

`https://www.macrosight.net/trade-alert/?api=https://trade-alert-api.macrosight.net`
