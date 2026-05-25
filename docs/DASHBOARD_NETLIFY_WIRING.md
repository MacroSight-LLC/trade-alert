# Netlify dashboard wiring (www.macrosight.net/trade-alert/)

Your marketing site hosts the UI (`public/trade-alert/index.html`, commit `e65ca4e`).
The trade-alert **API** runs on Hetzner (`dashboard_api.py` on port 8080).

```
Netlify (UI)                         Hetzner (API)
─────────────────────────            ─────────────────────────────
www.macrosight.net/trade-alert/  →   trade-alert-api.macrosight.net
  index.html                           Caddy/nginx → dashboard:8080
  CSP connect-src allows API           DASHBOARD_CORS_ORIGINS=…
```

## Phase A — Now (API offline)

Expected behavior:

| UI state | Cause |
|----------|--------|
| Auth gate visible | Normal — user enters key (stored in `localStorage`) |
| Badge **API Offline** | `trade-alert-api.macrosight.net` has no DNS / VPS yet |
| Offline mode | UI should not throw; skip data fetches when probe fails |

**Local test** against a running API:

```bash
# trade-alert repo
uv run uvicorn dashboard_api:app --host 0.0.0.0 --port 8080
```

Open:

`https://www.macrosight.net/trade-alert/?api=http://localhost:8080`

(Only works if you tunnel or disable CORS for dev — for local UI use `file://` or Netlify preview with API on same machine + ngrok.)

Better local test: serve UI from trade-alert:

```bash
uv run uvicorn dashboard_api:app --port 8080
# http://localhost:8080/ serves dashboard.html with same-origin API
```

## Phase B — Hetzner live (production wiring)

### 1. DNS

| Record | Type | Target |
|--------|------|--------|
| `trade-alert-api.macrosight.net` | A | Hetzner public IPv4 |

(Netlify stays on `www.macrosight.net` — do **not** point www at Hetzner.)

### 2. VPS reverse proxy

Use [`deployment/caddy-trade-alert-api.caddy`](../deployment/caddy-trade-alert-api.caddy):

```caddy
trade-alert-api.macrosight.net {
    reverse_proxy 127.0.0.1:8080
}
```

Ensure `docker-compose.prod.yml` dashboard is up and bound to localhost:

```bash
docker compose -f docker-compose.prod.yml up -d dashboard
curl -s http://127.0.0.1:8080/health   # {"status":"ok"}
```

### 3. Dashboard env (Vault / `.env`)

```bash
DASHBOARD_API_KEY=<openssl rand -hex 32>
DASHBOARD_REQUIRE_AUTH=true
DASHBOARD_CORS_ORIGINS=https://www.macrosight.net
DASHBOARD_BIND=127.0.0.1
```

**Production VPS (37.27.184.125):** Caddy runs via `docker compose -f docker-compose.prod.yml up -d caddy`
(host networking, auto-TLS). Ensure `DASHBOARD_CORS_ORIGINS` is **not** left at `localhost:3000`.

Restart dashboard after setting vars.

### 4. Netlify CSP + server-side proxy (`my-site-3` repo)

The live UI at `/trade-alert/` uses **Netlify Functions** so visitors never enter an API key:

```
Browser → /trade-alert/api/summary → Netlify Function → Hetzner API (+ X-API-Key)
```

Set in **Netlify → Site → Environment variables**:

| Variable | Value |
|----------|--------|
| `DASHBOARD_API_KEY` | Same as Hetzner `~/trade-alert/.env` |
| `TRADE_ALERT_API_BASE` | `https://trade-alert-api.macrosight.net` (optional) |

CSP `connect-src` is `'self'` only (no direct browser calls to Hetzner). See `docs/TRADE_ALERT_DASHBOARD.md` in the website repo.

### 5. Netlify CSP (`netlify.toml` on website repo) — legacy direct-browser mode

Allow the browser to `fetch()` the API from your static page:

```toml
[[headers]]
  for = "/trade-alert/*"
  [headers.values]
    Content-Security-Policy = """
      default-src 'self';
      script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;
      style-src 'self' 'unsafe-inline';
      img-src 'self' data:;
      connect-src 'self' https://trade-alert-api.macrosight.net;
      font-src 'self';
      frame-ancestors 'none';
      base-uri 'self';
    """.replace("\n", " ")
```

Adjust `script-src` if you use other CDNs. **Without `connect-src` including the API host, the page stays "API Offline" even when Hetzner is up.**

### 5. Default API URL in your page

In `public/trade-alert/index.html`, set the production API base (one of):

```html
<meta name="trade-alert-api-base" content="https://trade-alert-api.macrosight.net">
```

```javascript
const DEFAULT_API_BASE = 'https://trade-alert-api.macrosight.net';
```

Resolution order (recommended, matches trade-alert `dashboard.html`):

1. `?api=` query param (one-time override, save to `localStorage`)
2. `localStorage.trade_alert_api_base`
3. `<meta name="trade-alert-api-base">`
4. Fallback → offline mode

## API contract (for your custom UI)

Base: `https://trade-alert-api.macrosight.net`

| Probe | Auth | Purpose |
|-------|------|---------|
| `GET /health` | No | Liveness — use for **online/offline** badge |
| `GET /api/auth/test` | `X-API-Key` | Validate key before loading data |

All `/api/*` routes require `X-API-Key` when `DASHBOARD_REQUIRE_AUTH=true`.

```javascript
const API_BASE = 'https://trade-alert-api.macrosight.net';
const headers = { 'X-API-Key': apiKey };

async function probeOnline() {
  const r = await fetch(`${API_BASE}/health`, { method: 'GET' });
  return r.ok;
}

async function validateKey(apiKey) {
  const r = await fetch(`${API_BASE}/api/auth/test`, { headers: { 'X-API-Key': apiKey } });
  return r.ok;
}
```

### Endpoints for 4 KPI cards + alerts table

| Your UI | Endpoint | Key fields |
|---------|----------|------------|
| Alerts today / total | `GET /api/summary` | `alerts_today`, `total_alerts` |
| Win rate (30d) | `GET /api/summary` | `overall_winrate`, `wins`, `losses` |
| Gate rejection rate | `GET /api/kpis` | `gate_rejection_rate` |
| Redis / system status | `GET /api/health` | `redis_ok`, `redis_latency_ms`, `status` (`live` / `degraded`) |
| Alerts table | `GET /api/alerts?limit=50` | array: `symbol`, `direction`, `edge_probability`, `confidence`, `entry`, `outcome`, `created_at`, … |

Optional charts (trade-alert full dashboard):

- `GET /api/winrate`
- `GET /api/frequency?days=30`
- `GET /api/symbols?limit=20`
- `GET /api/session-stats?timeframe=15m&date=today`

Poll interval: read `poll_interval_ms` from `/api/kpis` (default 30000).

### Example fetch wrapper

```javascript
async function apiGet(path, apiKey) {
  const res = await fetch(`${API_BASE}/api${path}`, {
    headers: apiKey ? { 'X-API-Key': apiKey } : {},
  });
  if (res.status === 401) throw new Error('Invalid API key');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
```

## Verification checklist

```bash
# 1. API up
curl -s https://trade-alert-api.macrosight.net/health

# 2. Auth
curl -s -H "X-API-Key: $DASHBOARD_API_KEY" \
  https://trade-alert-api.macrosight.net/api/summary | head

# 3. CORS (must include Access-Control-Allow-Origin: https://www.macrosight.net)
curl -s -D - -o /dev/null \
  -H "Origin: https://www.macrosight.net" \
  -H "Access-Control-Request-Method: GET" \
  -X OPTIONS \
  https://trade-alert-api.macrosight.net/api/health
```

Browser: open https://www.macrosight.net/trade-alert/ → enter real `DASHBOARD_API_KEY` → badge should show **LIVE** or **DEGRADED** (not Offline).

## Auth gate note

For production, **do not** accept arbitrary keys — call `/api/auth/test` after the user submits the key. Offline mode is only for when `/health` fails (API unreachable), not for invalid keys.

## Related

- [DASHBOARD_STATIC_HOSTING.md](./DASHBOARD_STATIC_HOSTING.md) — generate/sync alternate UI from trade-alert repo
- [`dashboard_api.py`](../dashboard_api.py) — source of truth for routes
