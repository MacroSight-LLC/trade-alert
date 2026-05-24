# Trade-alert follow-ups

GitHub Issues are **disabled** on this repository (`gh issue create` returns
repository has disabled issues), so persistent action items live here. To track
in GitHub when issues are enabled: label `tech-debt`, migrate open FU-* entries,
then replace this file with a short redirect stub.

Add new entries at the top with a date and a clear owner / exit condition.

When an item lands, move it under "Resolved" with the date and the PR or
commit that closed it.

---

## Open

### FU-013 — CUGA tool_call_tracking e2e in CI

**Status:** OPEN (2026-05-24)
**Component:** `tests/integration/test_tool_call_tracking.py`
**Action:** `TestE2EToolCallTracking` skipped in CI (requires live digital_sales + registry). Wire to compose test profile or remove skip when upstream stack is available in CI.
**Acceptance:** E2E class runs green in `trade-alert-tests` integration job without `-m "not e2e"`.

---

### FU-012 — Raise stability-tests pass threshold to 95%

**Status:** OPEN (2026-05-23)
**Component:** `.github/workflows/stability-tests.yml`
**Action:** Run `stability-tests.yml` via `workflow_dispatch` and record pass rate for Python 3.11, 3.12, 3.13, and Windows jobs. Raise threshold from 88% to 95% only when all four jobs report ≥95%.
**Acceptance:** Baseline recorded; threshold updated or item remains open until upstream CUGA suite is ready.

---

### FU-007 — pg_partman alerts partitioning

**Status:** READY_FOR_OPS_REVIEW (2026-05-23)
**Component:** `schema.sql` lines 176–199, `scripts/enable_partitioning.sql`
**Action:** Ops review retention vs `scripts/purge_old_data.py`, then run `scripts/enable_partitioning.sql` on prod Postgres during a maintenance window.
**Acceptance:** New months auto-partition; purge script updated for partitioned semantics before cutover.

**Repo prep:** `scripts/enable_partitioning.sql` created; `# PARTITIONED TABLE NOTE` in `purge_old_data.py`; `tests/unit/test_schema_partitioning.py` added (commit `2517a79`).

---

## Resolved

### FU-006 — TimesFM MCP prod verification

**Resolved:** 2026-05-24 — `5188bc3`
**Note:** TimesFM `/health` returned 200 on Hetzner prod; 15m orchestrator produced **8 forecast snapshots** (Langfuse trace `3b8791f8-01e6-49d5-b9b9-9b2e3bbbc0eb`). Prod fixes: writable HF cache volume + `TIMESFM_HF_REPO=google/timesfm-1.0-200m-pytorch` (2.5 safetensors incompatible with pinned loader). e2e exclusion removed from integration CI.

---

### FU-003 — Enable `VAULT_REQUIRED=true` for production deployments

**Resolved:** 2026-05-24 — `80c5f91`
**Note:** `verify-vault-required.sh` exit 0 on Hetzner prod (`37.27.184.125`). `VAULT_REQUIRED=true` confirmed in cuga container; Vault unsealed; no env-file fallback warnings. Loader: `deployment/load-prod-env.sh`.

---

### FU-002 — Sonnet 4 → 4.5 end-to-end output validation

**Resolved:** 2026-05-24 — `80c5f91`
**Note:** Prod traces confirming `claude-sonnet-4-5` via LiteLLM + `validate-sonnet-4-5.sh`:

| Timeframe | Langfuse trace ID | Model |
| --------- | ----------------- | ----- |
| 15m | `d9106d73-825d-4435-92b6-2ff598b4d61b` | claude-sonnet-4-5 |
| 1h | `ea51b5d2-a8ed-41db-863a-016e7a7c5662` | claude-sonnet-4-5 |

Gate mix within baseline; 0 alerts fired (valid off-hours / no-signal cycle).

---

### FU-011 — outcome_tracker.py decomposition

**Resolved:** 2026-05-23 — `80e2371`
**Note:** Postgres helpers extracted to `outcome_queries.py`; `outcome_tracker.py` slimmed to orchestration (~12KB).

---

### FU-010 — validate_and_filter.py decomposition

**Resolved:** 2026-05-23 — `80e2371`
**Note:** Extracted `gates/types.py`, `gates/reconciliation.py`, `gates/redis_circuit.py`, `gates/candidate.py`; orchestrator ~20KB; public API unchanged; 222+ gate tests green.

---

### FU-008 — prompt_manager.py decomposition

**Resolved:** 2026-05-23 — `80e2371`
**Note:** Split into `prompt_fetcher.py`, `prompt_renderer.py`, thin `prompt_manager.py` (all ≤20KB); `test_prompt_manager.py` unchanged.

---

### FU-009 — Mypy cleanup sprint

**Resolved:** 2026-05-23 — `6be84a9`
**Note:** 41 annotation errors fixed; `uv run mypy --config-file pyproject.toml` exits 0; CI merge gate enabled in `trade-alert-tests.yml`.

---

### FU-005 — Document implicit gate input contracts for test authors

**Resolved:** 2026-05-23
**Note:** `# TESTING NOTE` blocks added in `validate_and_filter.py` (sources_agree
reconciliation + HIGH_CONFIDENCE_ALIGNMENT). "Reconciliation pipeline" section
added to [`CONTRIBUTING.md`](./CONTRIBUTING.md). Extended gate tests in
`tests/unit/test_validate_and_filter_extended.py`.

---

### FU-004 — Authoritative `detect-secrets` rescan

**Resolved:** 2026-05-23
**Note:** `.secrets.baseline` regenerated with IBM fork `0.13.1+ibm.64.dss`.
CI drift check added to `.github/workflows/trade-alert-tests.yml` via pre-commit.

---

### FU-001 — Relocate CUGA-internal unit tests to upstream test tree

**Resolved:** 2026-05-22
**Note:** Moved to `tests/cuga_upstream/` — see that directory's README
for upstream migration instructions.
