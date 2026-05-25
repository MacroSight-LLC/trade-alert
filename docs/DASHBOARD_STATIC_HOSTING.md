# Static dashboard hosting (www.macrosight.net/trade-alert/)

Split hosting: **static UI** on your marketing site, **API + data** on the trade-alert VPS.

```
┌─────────────────────────────┐     HTTPS + CORS      ┌──────────────────────────┐
│ www.macrosight.net          │  ──────────────────►  │ trade-alert-api.         │
│ /trade-alert/index.html     │     X-API-Key         │ macrosight.net :8080     │
│ (static file)               │                       │ dashboard_api.py         │
└─────────────────────────────┘                       └──────────────────────────┘
```

## 1. Generate the static page

```bash
TRADE_ALERT_API_BASE=https://trade-alert-api.macrosight.net ./scripts/sync_static_dashboard.sh
```

Upload `static/trade-alert/index.html` to your site so it is served at:

**https://www.macrosight.net/trade-alert/**

(No build step — single HTML file with inline CSS and Chart.js CDN.)

## 2. Expose the API on the VPS

The dashboard container listens on **8080** (default bind `127.0.0.1:8080` in `docker-compose.prod.yml`).

Put a reverse proxy in front (Caddy or nginx) on the same Hetzner node:

### Caddy example

```caddy
trade-alert-api.macrosight.net {
    reverse_proxy 127.0.0.1:8080
}
```

Point DNS `trade-alert-api.macrosight.net` → Hetzner public IP.

## 3. Production env (VPS / Vault)

```bash
DASHBOARD_API_KEY=<generate-with-openssl-rand-hex-32>
DASHBOARD_REQUIRE_AUTH=true
DASHBOARD_CORS_ORIGINS=https://www.macrosight.net
```

Add to `.env` / Vault seed and restart the `dashboard` service:

```bash
docker compose -f docker-compose.prod.yml up -d dashboard
```

## 4. Verify

```bash
# API (replace key)
curl -s -H "X-API-Key: $DASHBOARD_API_KEY" https://trade-alert-api.macrosight.net/api/summary

# CORS preflight from browser origin
curl -s -X OPTIONS \
  -H "Origin: https://www.macrosight.net" \
  -H "Access-Control-Request-Method: GET" \
  -i https://trade-alert-api.macrosight.net/api/health
```

Open **https://www.macrosight.net/trade-alert/** — enter the API key when prompted.

## 5. Same-origin alternative

If you prefer one hostname, proxy both paths on the VPS:

- `/trade-alert/` → static files
- `/api/` → dashboard:8080

Then set `trade-alert-api-base` meta to `https://www.macrosight.net` or leave empty when HTML is served from the same host as the API.

## Local dev

- UI + API together: `uvicorn dashboard_api:app --port 8080` → http://localhost:8080/
- Static UI against local API: open `static/trade-alert/index.html` via a static server and set `?api=http://localhost:8080`
