# Trade-alert follow-ups

GitHub Issues are disabled on this repository, so persistent action items
that are too small for a CHANGELOG entry but too important to lose track
of live here. Add new entries at the top with a date and a clear owner /
exit condition.

When an item lands, move it under "Resolved" with the date and the PR or
commit that closed it.

---

## Open

### FU-001 — Relocate CUGA-internal unit tests to upstream test tree

**Opened:** 2026-05-22
**References:** [SSOT §6 Notes](./CUGA-Trading-Alert-System-SPEC-v1.3.md#6-directory-layout-for-trade-alert)

A handful of files under `tests/unit/` exercise CUGA library internals
living under `src/cuga/`, not trade-alert code. They are skipped in
[`.github/workflows/trade-alert-tests.yml`](.github/workflows/trade-alert-tests.yml)
via `--ignore` so they do not run in CI, but they still live in this repo
and confuse contributors who try to run the full suite locally.

**Affected files**
- `tests/unit/test_llm_override.py`
- `tests/unit/test_plan_controller_prompt.py`
- `tests/unit/test_variables_manager_langgraph.py`
- `tests/unit/test_variables_manager_with_state.py`

These test phantom modules (`variables_manager.py`, `forecast_gate.py`,
`llm_override.py`, `plan_controller_prompt.py`) that do not exist at the
trade-alert root — they exist under `src/cuga/...`.

**Action**
1. Open an upstream PR against the CUGA repository to add these four test
   files under that repo's test tree.
2. Once accepted upstream, delete them from `trade-alert/tests/unit/`.
3. Drop the corresponding `--ignore=` lines from
   `.github/workflows/trade-alert-tests.yml`.
4. Mark this entry resolved with the upstream PR link.

**Exit condition:** all four files removed from this repo and the
`--ignore=` flags removed from `trade-alert-tests.yml`.

---

## Resolved

_(none yet)_
