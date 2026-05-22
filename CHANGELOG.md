# Changelog

All notable changes to the trade-alert project follow this changelog.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows the phase ladder in
[`CUGA-Trading-Alert-System-SPEC-v1.3.md`](./CUGA-Trading-Alert-System-SPEC-v1.3.md) §0.

Dates without a verifiable git tag are marked _approximate_; they reflect the
phase ordering in the SSOT phase table.

## [Unreleased]

### Changed
- SSOT §0 marks `SSOT.md` as a canonical symlink to
  `CUGA-Trading-Alert-System-SPEC-v1.3.md`.
- SSOT §3 MCP table now lists port 8012 (TimesFM MCP). §2 caption updated to
  reflect 12 MCPs and 7 collectors.
- SSOT §4 schema blocks match `models.py` exactly: `Signal.type` includes
  `price_forecast`; `Snapshot.timestamp` and `TraceAnalysis.timestamp` are
  `AwareDatetime`; `PlaybookAlert.entry` is enforced via a model validator with
  direction-aware ordering (LONG: `stop < level < target`; SHORT:
  `target < level < stop`; WATCH skips). `PlaybookAlert` rejects
  `edge_probability > 0.85` with `confidence < 0.15`.
- SSOT §6 directory layout now includes `constants.py`, `eod_summary.py`,
  `execution_mapper.py`, `execution_trigger.py`, `execution_webhook.py`,
  `log_config.py`, `metrics.py`, `normalizers/forecast_normalizer.py`,
  `docker-compose.test.yml`, `docker/Dockerfile.timesfm`,
  `workflows/collector-forecast.yaml`, `workflows/state-summary.yaml`, and
  `docs/design.html`.
- `merger.py` reads `Snapshot.timestamp` as a `datetime` directly instead of
  re-parsing it from a string.
- Tests reorganised: orphan `tests/*.py` files moved into `tests/unit/` or
  `tests/integration/`; `tests/integration_smoke.py` moved into
  `tests/integration/`.
- `docker-compose.prod.yml` no longer ships dev/default fallbacks for
  `VAULT_TOKEN`, `SALT`, `LANGFUSE_DB_PASSWORD`, or `GRAFANA_ADMIN_PASSWORD`;
  required variables now fail-fast via `${VAR:?...}` syntax.
- Orchestrator `pipeline-summary` steps in both `orchestrator-15m.yaml` and
  `orchestrator-1h.yaml` now emit a `trace_health` boolean alongside the
  existing fields.
- `design.html` moved to `docs/design.html`.

### Added
- [`CHANGELOG.md`](./CHANGELOG.md) (this file).
- [`docker-compose.test.yml`](./docker-compose.test.yml) for CI integration
  tests (Redis + Postgres only; MCPs mocked in fixtures).
- `tests/integration/__init__.py` and `tests/system/__init__.py` package
  markers.
- Documentation polish: `CLAUDE.md` AI guardrails + key-files table;
  `README.md` documentation section now self-identifies; `CONTRIBUTING.md`
  "Adding new source files" + "Secrets baseline" sections;
  `SETUP_AND_OPERATIONS.md` "Last verified against" header.

## [1.0.0] - 2026-03-11

### Added
- Tagged 1.0.0 milestone after Phase 10 hardening (Vault, data quality gates,
  CI polish).

## [0.10.0] - 2026-03-12 (approximate; tag pending)

### Added
- Phase 10 — pipeline hardening: HashiCorp Vault server mode with file backend
  and auto-unseal; vault_env_loader auto-injects secrets at import time with
  retry + exponential backoff; data-quality gates; gate-config fixes.

## [0.9.0] - 2026-03-09 (approximate; tag pending)

### Added
- Phase 9 — analytics web UI: `dashboard_api.py` and `dashboard.html`.

## [0.8.1] - 2026-03-08 (approximate; tag pending)

### Fixed
- Polish phase: Docker fixes, CI corrections, additional tests, environment
  variable extraction cleanups.

## [0.8.0] - 2026-03-07

### Added
- Phase 8 — outcome tracker and winrate reporting (`outcome_tracker.py`,
  `winrate_injector.py`, schema additions).

## [0.7.0] - 2026-03-05 (approximate; tag pending)

### Added
- Phase 7 — orchestration, `healthcheck.py`, Docker images
  (`docker/Dockerfile.cuga`, `docker/Dockerfile.mcp`), crontab driving the
  15m and 1h pipelines.

## [0.6.0] - 2026-03-03 (approximate; tag pending)

### Added
- Phase 6 — notifier with Discord rich embeds and candlestick chart
  attachments; Postgres alert logging (`notifier_and_logger.py`,
  `chart_gen.py`).

## [0.5.0] - 2026-03-01 (approximate; tag pending)

### Added
- Phase 5 — decision engine workflows: `workflows/decision-15m.yaml`,
  `workflows/decision-1h.yaml`, Claude Sonnet 4 prompts via
  `prompt_manager.py`, 7-gate `validate_and_filter.py`.

## [0.4.0] - 2026-02-27 (approximate; tag pending)

### Added
- Phase 4 — merger and Postgres DB layer (`merger.py`, `db.py`, `schema.sql`).

## [0.3.0] - 2026-02-24 (approximate; tag pending)

### Added
- Phase 3 — sentiment and macro collectors and normalizers
  (`workflows/collector-sentiment.yaml`, `workflows/collector-macro.yaml`,
  `normalizers/sentiment_normalizer.py`, `normalizers/macro_normalizer.py`).

## [0.2.0] - 2026-02-21 (approximate; tag pending)

### Added
- Phase 2 — TA collector and normalizer (`workflows/collector-ta.yaml`,
  `normalizers/ta_normalizer.py`).

## [0.1.0] - 2026-02-18 (approximate; tag pending)

### Added
- Phase 1 — core Pydantic v2 models (`models.py`), Redis snapshot queues
  (`redis_client.py`), initial collector scaffolding.
