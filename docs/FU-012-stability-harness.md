# FU-012: Stability harness restore

Track re-enabling CUGA stability tests on `main` after the harness was removed
during the stabilization sprint.

## Checklist

- [ ] Vendor or restore `run_stability_tests.py`
- [ ] Add `stability_config.yml` (baseline pass rate ≥88%, target 95%)
- [ ] Restore `.github/workflows/stability-tests.yml` on `push: main`
- [ ] Set artifact upload `if-no-files-found: warn`
- [ ] Verify green run before raising threshold

## Context

- `stability-tests.yml` was **deleted** (not parked on `workflow_dispatch`)
- Deferred from gate refactor PR2 (commit `999dc77`)
- `src/cuga/` remains read-only per SSOT
