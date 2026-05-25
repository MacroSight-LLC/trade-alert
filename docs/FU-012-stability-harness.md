# FU-012: Stability harness restore

GitHub: https://github.com/MacroSight-LLC/trade-alert/issues/14

Track re-enabling CUGA stability tests on `main` after the harness was removed
during the stabilization sprint.

## Checklist

- [x] Vendor or restore `run_stability_tests.py`
- [x] Restore `.github/workflows/stability-tests.yml` on `push: main` + `workflow_dispatch`
- [x] Set artifact upload `if-no-files-found: warn`
- [ ] Raise pass-rate threshold from 80% → 88% → 95% after green baseline runs

## Context

- Upstream CUGA e2e harness replaced with trade-alert-specific harness measuring
  `validate_and_filter` pass rate, latency p50/p95/p99, and Redis circuit-breaker trips
- `stability-tests.yml` was **deleted** 2026-05-25; restored in PR8 refactor track
- `src/cuga/` remains read-only per SSOT

## Usage

```bash
uv run run_stability_tests.py --method local --iterations 50
```

Writes `stability_results.json` (or `TEST_RESULTS_FILE` env override).
