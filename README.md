# trade-alert

> Production trading alert engine built on [CUGA](./README.cuga.md).

11 MCP ensemble (TA · flow · sentiment · options · insider · macro · EDGAR · short interest) → Claude Sonnet 4 probabilistic reasoning → Discord trade playbooks with candlestick charts, entry, stop, target, thesis & edge probability.

## Documentation
- **Full spec & architecture:** [`CUGA-Trading-Alert-System-SPEC-v1.3.md`](./CUGA-Trading-Alert-System-SPEC-v1.3.md)
- **Setup & operations:** [`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md)
- **CUGA upstream docs:** [`README.cuga.md`](./README.cuga.md)

## Stack Overview

| Layer                 | Components                                                                                                                                                                              |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Data (11 MCPs)**    | TradingView (:8001), Polygon (:8002), Discord (:8003), Finnhub (:8004), ROT (:8005), EDGAR (:8006), YFinance (:8007), Trading (:8008), FRED (:8009), SpamShield (:8010), Alpaca (:8011) |
| **Pipeline**          | 6 collectors → merger → Claude Sonnet 4 decision → 7-gate validate & filter → notifier                                                                                                  |
| **Signal types (10)** | `technical_trend`, `volume_spike`, `sentiment_bull`, `sentiment_bear`, `options_flow`, `insider_activity`, `relative_strength`, `macro_risk_off`, `catalyst_event`, `short_interest`    |
| **Infra**             | Redis (snapshot queues), Postgres (alert logging), Vault (secrets, file backend), Langfuse (prompt mgmt + tracing)                                                                      |
| **Output**            | Discord embeds with mplfinance candlestick charts, entry/stop/target overlays                                                                                                           |
| **Ops**               | Discord bot (`!scan`, `!status`, `!last`), cron scheduler, analytics dashboard (:8080)                                                                                                  |

**20 containers total** — all orchestrated via `docker-compose.prod.yml`.

## Quick Start

### 1. Prerequisites
- Python 3.11+
- Docker & Docker Compose
- API keys: Anthropic, Finnhub, FRED, Polygon.io, Alpaca, Discord bot token

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
pip install pydantic httpx psycopg2-binary redis pytest
pytest tests/test_validate_and_filter.py -v   # 45 tests
```

## Project Structure

| File                     | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `models.py`              | Pydantic schemas: Signal, Snapshot, PlaybookAlert              |
| `merger.py`              | Deduplicates & ranks snapshots from Redis                      |
| `validate_and_filter.py` | 7-gate server-side filter (VIX, R:R, EP ceiling, etc.)         |
| `notifier_and_logger.py` | Discord embeds + candlestick charts + Postgres logging         |
| `chart_gen.py`           | mplfinance candlestick PNG generation from Polygon data        |
| `db.py`                  | Postgres insert/update/query for alerts table                  |
| `pipeline_runner.py`     | Generic YAML workflow engine (collectors → decision)           |
| `prompt_manager.py`      | Langfuse-first prompt loading with 300s cache + fallback       |
| `discord_bot.py`         | Discord ops bot (`!scan`, `!status`, `!last`, `!help`)         |
| `healthcheck.py`         | Redis/Postgres/MCP health checks + JSONL logging               |
| `outcome_tracker.py`     | Resolves open alerts via Polygon/Finnhub price polling         |
| `alert_quality.py`       | Per-alert quality scoring (5 sub-scores)                       |
| `winrate_injector.py`    | Injects historical win-rate calibration into prompts           |
| `dashboard_api.py`       | FastAPI analytics dashboard (port 8080)                        |
| `vault_env_loader.py`    | Auto-loads Vault secrets into `os.environ` on import           |
| `normalizers/`           | 7 normalizers (TA, flow, sentiment, market, macro, events, SI) |
| `workflows/`             | CUGA YAML workflows (6 collectors, 2 decisions, orchestrators) |
| `deployment/`            | Vault config (HCL), auto-unseal entrypoint                     |

## Testing

```bash
# Unit tests (45 gate tests)
pytest tests/test_validate_and_filter.py -v

# With coverage
pytest tests/ --cov=. --cov-report=term-missing

# Integration smoke test (needs Docker)
python tests/integration_smoke.py
```
