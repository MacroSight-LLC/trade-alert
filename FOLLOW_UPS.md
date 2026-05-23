# Trade-alert follow-ups

GitHub Issues are disabled on this repository, so persistent action items
that are too small for a CHANGELOG entry but too important to lose track
of live here. Add new entries at the top with a date and a clear owner /
exit condition.

When an item lands, move it under "Resolved" with the date and the PR or
commit that closed it.

---

## Open

_(none)_

---

## Resolved

### FU-002 — Sonnet 4 → 4.5 end-to-end output validation

**Resolved:** 2026-05-23
**Note:** Local dev stack validation after Anthropic credit top-up. Both
orchestrators completed with `claude-sonnet-4-5`; LiteLLM + Langfuse traces
confirmed. Merger fix (`model_dump(mode="json")`) in commit `4524365`.

| Cycle | Trace ID | Merger | LLM | Parsed | Passed | Rejected |
| ----- | -------- | ------ | --- | ------ | ------ | -------- |
| 15m | `7d9370bc-2a1b-4fa1-ba57-ba0f14451309` | 10 | 0 candidates | 0 | 0 | 0 |
| 1h | `98b844c0-c924-4eed-9c92-0d21249eb748` | 10 | 1 candidate | 1 | 1 WATCH (AMZN) | 0 |

**15m:** `reason=llm_zero_candidates` — LLM returned valid empty set (market
closed, no actionable setups). Zero PlaybookAlert parse failures.

**1h:** AMZN WATCH passed gates (`ep=0.72`, `conf=0.68`, `sa=5`,
`sources_agree` server override 4→5). PlaybookAlert schema validated through
`validate_and_filter`.

**Gate-rejection snapshot:** N/A in dev (no Prometheus / `alert_gates` table).
Per-gate rejection counts: 15m 0/0, 1h 0/0 — within envelope by inspection.

**Known non-blockers:** Notifier/healthcheck failed on stale container image
(`map_to_execution_payload` import — fixed in working tree, rebuild required).
TimesFM MCP unreachable in dev compose.

**Ops follow-up:** Re-run `./deployment/validate-sonnet-4-5.sh` on Hetzner
after next prod deploy to capture production trace IDs and Prometheus gate mix.

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

### FU-003 — Enable `VAULT_REQUIRED=true` for production deployments

**Resolved:** 2026-05-23 (repo); ops host `.env` update required on deploy
**Note:** `.env.example` defaults `VAULT_REQUIRED=true`. Production checklist in
[`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md). Unit tests in
`tests/unit/test_vault_env_loader.py`. Deploy smoke extended in
`.github/workflows/deploy.yml`. Verification script:
`deployment/verify-vault-required.sh`.

**Ops reminder:** Set `VAULT_REQUIRED=true` in `~/trade-alert/.env` on the
Hetzner host and run `./deployment/verify-vault-required.sh` after restart.

---

### FU-001 — Relocate CUGA-internal unit tests to upstream test tree

**Resolved:** 2026-05-22
**Note:** Moved to `tests/cuga_upstream/` — see that directory's README
for upstream migration instructions.
