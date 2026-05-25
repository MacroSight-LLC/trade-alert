"""Unit tests for run_stability_tests.py harness."""

from __future__ import annotations

import json
from pathlib import Path

from run_stability_tests import run_stability_suite


def test_stability_suite_passes_with_mock_redis(tmp_path: Path) -> None:
    results = run_stability_suite(iterations=5, symbols=("AAPL",))
    assert results["iterations"] == 5
    assert results["pass_rate"] >= 80.0
    assert "latency_seconds" in results
    assert results["passed"] is True

    out = tmp_path / "stability_results.json"
    out.write_text(json.dumps(results), encoding="utf-8")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["pass_count"] >= 0
