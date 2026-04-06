# trade-alert

> Production trading alert engine built on [CUGA](./README.cuga.md).

12 MCP ensemble (TA · flow · sentiment · options · insider · macro · EDGAR · short interest · forecast · time-series) → Claude Sonnet 4 probabilistic reasoning → 7-gate validation → Discord trade playbooks with candlestick charts, EMA/ATR overlays, entry, stop, target, thesis & edge probability.

## Documentation
- **Full spec & architecture:** [`CUGA-Trading-Alert-System-SPEC-v1.3.md`](./CUGA-Trading-Alert-System-SPEC-v1.3.md)
- **Setup & operations:** [`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md)
- **Contributing:** [`CONTRIBUTING.md`](./CONTRIBUTING.md)
- **CUGA upstream docs:** [`README.cuga.md`](./README.cuga.md)

## Stack Overview

| Layer                 | Components                                                                                                                                                                                                           |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Data (12 MCPs)**    | TradingView (:8001), Polygon (:8002), Discord (:8003), Finnhub (:8004), ROT (:8005), EDGAR (:8006), YFinance (:8007), Trading (:8008), FRED (:8009), SpamShield (:8010), Alpaca (:8011), TimesFM (:8012) |
| **Pipeline**          | 7 collectors → merger (time-decay, diversity scoring, composite signals) → Claude Sonnet 4 decision → 7-gate validate & filter → notifier                                                                           |
| **Signal types (11)** | `technical_trend`, `volume_spike`, `sentiment_bull`, `sentiment_bear`, `options_flow`, `insider_activity`, `relative_strength`, `macro_risk_off`, `catalyst_event`, `short_interest`, `price_forecast`                |
| **Infra**             | Redis (snapshot queues), Postgres (alert logging), Vault (secrets, file backend), Langfuse (prompt mgmt + tracing), Prometheus + Grafana (metrics)                                                                   |
| **Output**            | Discord embeds with mplfinance candlestick charts, EMA/ATR overlays, confidence color-coding, historical win-rate stats, tiered channel routing                                                                      |
| **Ops**               | Discord bot (`!scan`, `!status`, `!last`), cron scheduler, analytics dashboard (:8080), Prometheus (:9090) + Grafana (:3001) monitoring, pg-backup (daily pg_dump)                                                   |

**24 containers total** — all orchestrated via `docker-compose.prod.yml`.

### Production Hardening — Complete

| Phase | Scope |
|-------|-------|
| **Hardening** | Pydantic validators, 7-gate validation, NaN/Inf guards, AST-based exec sandbox, Redis connection pooling, persist-first ordering, atomic SET NX dedup, non-root Docker, resource limits, Postgres CHECK constraints & indexes, thread-safe Langfuse singleton |
| **Signal Quality** | Continuous interpolation scoring, graceful degradation, merger time-decay & diversity tuning, composite signal detection (VOLATILITY_CATALYST, VOLUME_CONFIRMED_BREAKOUT), EP calibration |
| **Validation Gates** | VIX hard/soft gates, forecast contradiction gate, volume confirmation, macro staleness guard, symbol hallucination detection, price-normalized micro-risk floor |
| **Alert Output** | Historical win-rate embeds, confidence color-coding, EMA/ATR overlays, truncate-safe fields, tiered channel routing |
| **Infrastructure** | Structured logging (JSON/text toggle), Prometheus counters/histograms/gauges, Grafana dashboards, Redis retry+backoff, HTTP connection pooling, Discord circuit breaker, market hours automation via exchange_calendars |
| **Feedback Loop** | Calibration accuracy tracking, Langfuse trace→outcome linkage, expiry rate monitoring, auto-promote golden datasets |

**620+ unit tests, 0 failures.**

## Quick Start

### 1. Prerequisites
- Python 3.11+
- Docker & Docker Compose
- API keys: Anthropic, Finnhub, FRED, Polygon.io, Alpaca, Discord bot token
- See [SETUP_AND_OPERATIONS.md](./SETUP_AND_OPERATIONS.md) for full walkthrough

### 2. Environment setup
```bash
cp .env.example .env.secrets
# Fill in all required values (API keys, Discord tokens, etc.)
# .env.secrets is git-ignored
```

### 3. Launch the full stack
```bash
set -a && source .env.secrets && set +a
docker compose -f docker-compose.prod.yml --profile mcp up -d
```

### 4. Initialize Vault & seed secrets
```bash
./scripts/vault-init.sh     # initializes, unseals, seeds .env.secrets → Vault KV v2
```

### 5. Seed Langfuse prompts
```bash
docker compose -f docker-compose.prod.yml exec cuga python scripts/seed_langfuse_prompts.py
```

### 6. Run unit tests
```bash
pip install -e ".[dev]"
pytest tests/unit/ -q   # 620+ tests
```

## Project Structure

| File / Directory         | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `models.py`              | Pydantic schemas: Signal, Snapshot, PlaybookAlert              |
| `pipeline_runner.py`     | Generic YAML workflow engine (collectors → decision)           |
| `merger.py`              | Deduplicates & ranks snapshots from Redis                      |
| `validate_and_filter.py` | 7-gate server-side filter (VIX, R:R, EP ceiling, etc.)         |
| `notifier_and_logger.py` | Discord embeds + candlestick charts + Postgres logging         |
| `chart_gen.py`           | mplfinance candlestick PNG generation from Polygon data        |
| `db.py`                  | Postgres connection pool, insert/update/query for alerts       |
| `prompt_manager.py`      | Langfuse-first prompt loading with 300s cache + YAML fallback  |
| `decision_helpers.py`    | Snapshot merging, quality scoring, dataset capture helpers      |
| `discord_bot.py`         | Discord ops bot (`!scan`, `!status`, `!last`, `!help`)         |
| `healthcheck.py`         | Redis/Postgres/MCP health checks + JSONL logging               |
| `outcome_tracker.py`     | Resolves open alerts via Polygon/Finnhub/TimesFM price polling |
| `alert_quality.py`       | Per-alert quality scoring (5 sub-scores) with calibration      |
| `winrate_injector.py`    | Injects historical win-rate calibration into prompts           |
| `dashboard_api.py`       | FastAPI analytics dashboard + Prometheus `/metrics` endpoint   |
| `vault_env_loader.py`    | Auto-loads Vault secrets into `os.environ` on import           |
| `constants.py`           | Centralized Redis keys, TTLs, market hours/holidays            |
| `redis_client.py`        | Singleton Redis client with retry + connection pooling         |
| `log_config.py`          | Structured logging config (JSON/text toggle via `LOG_FORMAT`)  |
| `metrics.py`             | Prometheus counters, histograms, gauges                        |
| `pipeline_tracing.py`    | Langfuse root trace + span management for pipeline runs        |
| `langfuse_client.py`     | Thread-safe singleton Langfuse client with graceful degradation|
| `langfuse_datasets.py`   | Auto-promote golden datasets, quality gating                   |
| `normalizers/`           | 8 normalizers (TA, flow, sentiment, market, macro, events, SI, forecast) |
| `workflows/`             | CUGA YAML workflows (7 collectors, 2 decisions, orchestrators) |
| `deployment/`            | Vault config, auto-unseal entrypoint, Prometheus config        |
| `schema.sql`             | Postgres schema: alerts table, indexes, CHECK constraints, views |

## Testing

```bash
# Full unit test suite (620+ tests)
pytest tests/unit/ -q

# With coverage
pytest tests/unit/ --cov=. --cov-report=term-missing

# Integration smoke test (needs Docker)
python tests/integration_smoke.py
```
