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

### FU-007 — pg_partman alerts partitioning

**Status:** DEFERRED (schema design)
**Component:** `schema.sql` lines 176–199
**Action:** Enable `pg_partman` monthly partitions on `alerts.created_at` after ops review of retention vs `scripts/purge_old_data.py`.
**Acceptance:** New months auto-partition; purge script documented for partitioned tables.

---

### FU-008 — prompt_manager.py decomposition

**Status:** DEFERRED (refactor)
**Component:** `prompt_manager.py` (~34KB)
**Action:** Split prompt fetch, template render, and gate-var injection into focused modules when touching prompts next.
**Acceptance:** No single file >20KB; tests unchanged.

---

### FU-009 — Mypy cleanup sprint

**Status:** DEFERRED
**Action:** Fix pre-existing mypy errors repo-wide without changing runtime behavior; keep `[tool.mypy] files` list as CI scope until clean.
**Acceptance:** `uv run mypy --config-file pyproject.toml` exits 0.

---

### FU-006 — TimesFM MCP prod verification

**Status:** OPEN
**Component:** `timesfm_mcp.py` / forecast collector
**Issue:** TimesFM MCP was unreachable in dev during v1.1.0 validation.
**Action:** On prod, run `healthcheck.py` with TimesFM endpoint enabled and confirm:
1. `/health` returns `{"timesfm": "ok"}`
2. A full 15m orchestrator run produces a non-null forecast field in at least one alert.
**Acceptance:** Logged prod trace shows `timesfm_forecast` key populated.

**Follow-on:** Once TimesFM is healthy on prod and the forecast collector path is validated
end-to-end, lift the integration CI e2e exclusion (`-m "not e2e"` in
`.github/workflows/trade-alert-tests.yml`) in a single line change.

---

### FU-002 — Sonnet 4 → 4.5 end-to-end output validation

**Status:** PENDING PROD VERIFICATION
**Action:** Re-run `./deployment/validate-sonnet-4-5.sh` on Hetzner prod host.
**Capture:** prod trace IDs from Langfuse + Prometheus gate mix (`gate_rejection_total` labels).
**Acceptance:** ≥1 live trace confirms `claude-sonnet-4-5` is being called; gate mix matches dev baseline within 5% per gate family.

**Local dev validation (2026-05-23):** Both orchestrators completed with `claude-sonnet-4-5`; LiteLLM + Langfuse traces confirmed. Merger fix (`model_dump(mode="json")`) in commit `4524365`.

| Cycle | Trace ID | Merger | LLM | Parsed | Passed | Rejected |
| ----- | -------- | ------ | --- | ------ | ------ | -------- |
| 15m | `7d9370bc-2a1b-4fa1-ba57-ba0f14451309` | 10 | 0 candidates | 0 | 0 | 0 |
| 1h | `98b844c0-c924-4eed-9c92-0d21249eb748` | 10 | 1 candidate | 1 | 1 WATCH (AMZN) | 0 |

---

### FU-003 — Enable `VAULT_REQUIRED=true` for production deployments

**Status:** PENDING PROD VERIFICATION
**Action:** Confirm `VAULT_REQUIRED=true` on prod host. Run:
`VAULT_REQUIRED=true ./deployment/verify-vault-required.sh`
**Acceptance:** Script exits 0 with no fallback-to-env-file warnings.

**Repo baseline (2026-05-23):** `.env.example` defaults `VAULT_REQUIRED=true`. Production checklist in [`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md). Unit tests in `tests/unit/test_vault_env_loader.py`. Deploy smoke in `.github/workflows/deploy.yml`.

---

## Resolved

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
