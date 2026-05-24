# Changelog

All notable changes to the trade-alert project follow this changelog.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows the phase ladder in
[`CUGA-Trading-Alert-System-SPEC-v1.3.md`](./CUGA-Trading-Alert-System-SPEC-v1.3.md) §0.

Dates without a verifiable git tag are marked _approximate_; they reflect the
phase ordering in the SSOT phase table.

## [Unreleased]

Nothing yet — see [1.2.0] below.

## [1.2.0] - 2026-05-23

Stabilization sprint: CI/CD hardening, module decomposition, test coverage, and ops prep.

### Added
- Deploy rollback sentinel (`/tmp/rollback_deploy_sha`) and smoke-failure rollback step in `deploy.yml`.
- TimesFM health unit test (`test_timesfm_health_check_included`) and `# FU-006` comment in `healthcheck.py`.
- Module decomposition: `prompt_fetcher.py`, `prompt_renderer.py`; `gates/types.py`, `gates/reconciliation.py`, `gates/redis_circuit.py`, `gates/candidate.py`; `outcome_queries.py`.
- `scripts/enable_partitioning.sql` and `tests/unit/test_schema_partitioning.py` (FU-007 ops prep).
- Unit tests: `test_gate_telemetry.py`, `test_gate_config.py`, `test_metrics.py`, `test_llm_client.py`, `test_llm_response_parser.py`.
- Merger Python-layer `model_dump(mode="json")` regression test (FU-002).
- `docs/architecture.md` root vs subdirectory map; `CONTRIBUTING.md` File Placement Guide.
- FU-012 tracking for stability-tests 95% threshold (deferred pending workflow_dispatch baseline).

### Changed
- Mypy is a merge gate in CI (FU-009); 41 annotation fixes across 20 files.
- `validate_and_filter.py` orchestration-only (~20KB); `prompt_manager.py` thin orchestrator (~11KB).
- Removed legacy `.github/workflows/tests.yml` (CUGA Playwright stub); `trade-alert-tests.yml` is authoritative.
- Relocated CUGA integration tests (`test_conversation_history`, `test_llm_config_publish`) to `tests/cuga_upstream/`.
- Integration CI: `# TODO: remove -m "not e2e" after FU-006 resolved` comment added.

### Fixed
- Normalizer timestamps use `datetime` objects (mypy-aligned; Pydantic coercion unchanged at runtime).
- Session gate tests: re-export `EXTENDED_HOURS_ALERTS_ENABLED` from `validate_and_filter`.
- Dashboard API integration tests: disable auth in fixture to avoid env pollution from Vault/CUGA imports.

### Previously in [1.2.0] (pre-stabilization)
- P0: SpamShield collector path via `parallel_tool_calls`; `resilience/mcp_error_handler.py`.
- P1: `workflows/orchestrator-base.yaml`; workflow jsonschema CI; purge scripts; `gate_config.py` SSOT.
- P2: Extended-hours confidence penalty; EOD cron stagger; `COMPLIANCE.md`.
- P3: `docs/architecture.md`; Dependabot pip groups; expanded ruff CI; Helm lint.

## [1.1.0] - 2026-05-23

### Added
- Exhaustive gate unit tests (`tests/unit/test_validate_and_filter_extended.py`) covering all
  23 `GateRejection` variants, regime classification, dynamic gates, EP ceiling, JSON parse
  robustness, WATCH decay, dedup, and Redis circuit breaker behavior.
- Gate-level alert deduplication (`DEDUP_SUPPRESSED`) with configurable TTL for directional
  and WATCH alerts; dedup keys reset on WATCH→directional graduation.
- Redis circuit breaker for WATCH decay path with `REDIS_CIRCUIT_OPEN` and
  `WATCH_DECAY_SKIPPED` Prometheus metrics.
- Hardened execution bridge: `ExecutionPayload` schema v1.0, DB `idempotency_key`, strict
  webhook ack handling, expiry checks before dispatch.
- Notifier decomposition: `discord_formatter.py`, `alert_logger.py`, `notifier.py` with
  embed limit enforcement and Discord 429 backoff.
- Dashboard API endpoints: `/api/health`, `/api/kpis`, `/api/session-stats`, `/api/circuit-breaker`
  with Redis response caching; modernized `dashboard.html` (Nexus tokens, 30s polling).
- Prompt token budget guard, per-(symbol,direction,timeframe) win-rate injection, ensemble
  decision Langfuse logging, and optional `DECISION_FALLBACK_MODEL`.
- CI: `uv sync --frozen --check` step, pre-commit uv-lock hook, Dependabot config.

### Fixed
- Relocate CUGA-internal unit tests to `tests/cuga_upstream/`
  (FU-001 resolved); CI now uses a single `--ignore=tests/cuga_upstream/`
  flag in [`trade-alert-tests.yml`](.github/workflows/trade-alert-tests.yml).
- Add an autouse `unittest.mock.MagicMock` Redis fixture and align
  fixture data with the current server-side gates
  (`HIGH_CONFIDENCE_ALIGNMENT`, deterministic source reconciliation,
  bear-aligned snapshots for SHORT cases) in
  [`tests/unit/test_validate_and_filter.py`](tests/unit/test_validate_and_filter.py)
  to clear 11 pre-existing unit-test failures; no production logic in
  [`validate_and_filter.py`](validate_and_filter.py) was touched.
- Sync `TestGateRejectionEnum::test_expected_members_exist` assertion
  with the current 22-member `GateRejection` enum.
- Silence a pre-existing F841 (`a = _alert(...)` in
  `TestRiskOffHighVixRegime::test_vix_soft_bypassed_for_risk_off_high_vix`)
  by renaming to `_a` per ruff's `dummy-variable-rgx`.
- Add `docker/Dockerfile.dashboard` build step to the `docker-build` job
  in [`trade-alert-tests.yml`](.github/workflows/trade-alert-tests.yml);
  verified the image builds cleanly.
- `uv.lock` Python alignment verified: `.python-version` (3.11),
  `uv.lock` `requires-python = ">=3.10, <3.14"`, and the CI workflow's
  `uv python install` all agree — no lockfile regeneration needed.
- `.secrets.baseline`: re-pointed the relocated
  `tests/cuga_upstream/test_llm_override.py` entry and refreshed the
  `generated_at` timestamp via the IBM detect-secrets hook.
- Mark the two well-known langfuse Docker dev defaults
  ([`docker-compose.yml`](docker-compose.yml) `POSTGRES_PASSWORD` and
  `DATABASE_URL`) with inline `# pragma: allowlist secret` comments —
  pre-existing dev fixtures that were never in the baseline (tracked
  for an authoritative rescan in FU-004).
- FU-002 closed: Sonnet 4.5 end-to-end validation (15m + 1h local cycles,
  PlaybookAlert parse through `validate_and_filter`; merger datetime fix in
  orchestrator YAML — `model_dump(mode="json")`). See [`FOLLOW_UPS.md`](./FOLLOW_UPS.md).
- Orchestrator merger datetime serialization (`model_dump(mode="json")` in
  `orchestrator-15m.yaml` / `orchestrator-1h.yaml`) — fixes zero-candidate
  merger failures from non-JSON-serializable `datetime` fields.

### Changed
- FU-003/004/005 repo work landed: `VAULT_REQUIRED=true` default and tests,
  authoritative `.secrets.baseline` + CI drift check, gate test-author docs in
  [`CONTRIBUTING.md`](./CONTRIBUTING.md). See [`FOLLOW_UPS.md`](./FOLLOW_UPS.md).
- [`docker-compose.yml`](docker-compose.yml): add `timesfm-mcp` (:8012) for
  full 12-MCP dev parity; expand `cuga` volume mounts to match prod hot-reload
  paths (`validate_and_filter.py`, `decision_helpers.py`, `metrics.py`, etc.).
- [`README.md`](README.md): align gate count wording with the 22-member
  `GateRejection` enum (was "7-gate").
- [`docker-compose.yml`](docker-compose.yml): strengthened the existing
  dev-compose banner with an explicit warning against using a production
  `.env` file (`VAULT_DEV_ROOT_TOKEN_ID` is a known insecure value).
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
- May 22 audit fixes (PR 1 — security + correctness):
  - `vault_env_loader.py` now honours `VAULT_REQUIRED=true` and raises
    `RuntimeError` on missing creds, auth failure, empty path, or read
    failure after 3 retries (was silently returning `0`).
  - `dashboard_api.py` rejects `*` in `DASHBOARD_CORS_ORIGINS` at startup.
  - `models.py` `Signal.raw` typed as `dict[str, Any]`; `TraceAnalysis`
    fields `cost_usd`/`latency_s`/`llm_calls`/`total_tokens` now declared
    with `ge=0`; `validate_entry` skips directional ordering for
    `direction == "WATCH"`; new proportional rule in
    `validate_edge_vs_confidence` rejects `ep >= 0.70` with
    `confidence < (1 - ep) * 0.5` (uses `math.isclose` to allow exact-floor
    values despite float drift). All `List`/`Dict` annotations standardised
    to lowercase.
  - SSOT §4 schema block, §10.1 model name, and the model-guardrails note
    mirror the above changes.
  - `models.py` smoke `__main__` block extracted to
    [`scripts/smoke_models.py`](./scripts/smoke_models.py).
  - SSOT §0.2 model references corrected to `Claude Opus 4.5`.
- May 22 audit fixes (PR 2 — cleanup):
  - Sonnet 4 model ID `claude-sonnet-4-20250514` (deprecated, retiring
    2026-06-15) bulk-replaced with `claude-sonnet-4-5` across all
    workflows, `pipeline_runner.py`, SSOT §10.1, and `README.md`. SSOT
    §10.1 documents the migration date and verification.
  - [`FOLLOW_UPS.md`](./FOLLOW_UPS.md) added to track persistent action
    items (GitHub Issues are disabled on this repo). FU-001 tracks the
    upstream relocation of CUGA-internal unit tests; SSOT §6 Notes now
    references it.
  - [`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md) gains a "Cron
    Schedule (live)" section documenting the actual market-hour-aware
    crontab (SSOT stays generic).
  - `docker-compose.yml` gains a 14-line dev-mode banner explaining the
    Vault dev-token, no-TLS, and resource-limit differences vs production.
  - Root `Dockerfile` renamed to
    [`docker/Dockerfile.dashboard`](./docker/Dockerfile.dashboard); both
    compose files and SSOT §6 updated.
  - CI now uses `astral-sh/setup-uv@v3` + `uv sync --frozen --group dev`
    instead of `pip install` of an explicit list, so `uv.lock` is enforced.
  - `.python-version` repinned to `3.11` to align local dev with CI,
    Dockerfile, mypy, and ruff (was `3.12.7`).
  - `ruff.toml` `select` extended with `I` (isort) and `UP` (pyupgrade);
    81 auto-fixes applied across the trade-alert file list and the three
    remaining `E402` import-order issues in `notifier_and_logger.py`
    fixed by hand.

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
