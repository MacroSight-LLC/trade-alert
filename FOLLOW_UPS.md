# Trade-alert follow-ups

GitHub Issues are disabled on this repository, so persistent action items
that are too small for a CHANGELOG entry but too important to lose track
of live here. Add new entries at the top with a date and a clear owner /
exit condition.

When an item lands, move it under "Resolved" with the date and the PR or
commit that closed it.

---

## Open

### FU-002 — Sonnet 4 → 4.5 end-to-end output validation

**Opened:** 2026-05-22
**References:** [`prompt_manager.py`](./prompt_manager.py),
[`validate_and_filter.py`](./validate_and_filter.py),
[`SETUP_AND_OPERATIONS.md`](./SETUP_AND_OPERATIONS.md) § Sonnet 4.5 End-to-End Validation

Code-side migration to `claude-sonnet-4-5` is complete. Runbook and validation
script are in place (`deployment/validate-sonnet-4-5.sh`). **Live execution
on production is still required** to close this item.

**Action (ops, on production host):**
1. Run 15m + 1h orchestrators (or `!scan 15m` / `!scan 1h`)
2. Execute `./deployment/validate-sonnet-4-5.sh` and complete manual checklist
3. Record Langfuse trace IDs and gate-rejection snapshot below
4. Move to Resolved when acceptance envelope passes

**Exit condition:** at least one full 15m + 1h pipeline cycle against
`claude-sonnet-4-5` whose `validate_and_filter` output JSON matches the
`PlaybookAlert` schema and whose gate-rejection mix is within the
historical envelope.

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
