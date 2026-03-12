"""trade-alert v0.8.0 — Integration Smoke Test.

Run with: python tests/integration_smoke.py

Infrastructure-dependent tests are skipped gracefully if
Docker/Postgres/Redis are not available.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

# Ensure repo root is on sys.path when running from tests/ subdirectory.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(_REPO_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SKIP_INFRA = False  # set True to skip all infrastructure tests


def check_docker() -> bool:
    """Return True if the docker CLI is available."""
    try:
        subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def test_imports() -> None:
    """Verify every Phase 1-8 Python module imports cleanly."""
    log.info("--- Test: Phase imports ---")
    from models import PlaybookAlert, Signal, Snapshot  # noqa: F401

    log.info("  PlaybookAlert, Signal, Snapshot ✅")

    from merger import get_macro_regime, merge  # noqa: F401

    log.info("  merger ✅")

    from db import (  # noqa: F401
        get_recent_alerts,
        get_winrate_by_bucket,
        insert_alert,
        update_outcome,
    )

    log.info("  db ✅")

    from notifier_and_logger import (  # noqa: F401
        compute_rr,
        format_embed,
        notify,
        send_ops_message,
    )

    log.info("  notifier_and_logger ✅")

    from healthcheck import check_mcps, check_postgres, check_redis, run_healthcheck  # noqa: F401

    log.info("  healthcheck ✅")

    from outcome_tracker import (  # noqa: F401
        evaluate_outcome,
        get_current_price,
        run_tracker_cycle,
    )

    log.info("  outcome_tracker ✅")

    log.info("All imports clean ✅")


def test_file_inventory() -> None:
    """Confirm all required Phase 1-8 files exist on disk."""
    log.info("--- Test: File inventory ---")
    required = [
        "models.py",
        "merger.py",
        "db.py",
        "schema.sql",
        "notifier_and_logger.py",
        "healthcheck.py",
        "outcome_tracker.py",
        "docker-compose.prod.yml",
        "docker/Dockerfile.cuga",
        "workflows/decision-15m.yaml",
        "workflows/decision-1h.yaml",
        "workflows/notifier.yaml",
        "workflows/orchestrator-15m.yaml",
        "workflows/orchestrator-1h.yaml",
        "workflows/outcome-tracker.yaml",
    ]
    missing = [f for f in required if not os.path.exists(f)]
    if missing:
        for f in missing:
            log.error("  MISSING: %s", f)
        sys.exit(1)
    log.info("  All %d required files present ✅", len(required))


def test_healthcheck_graceful() -> None:
    """Healthcheck must not crash even without Redis/Postgres."""
    log.info("--- Test: Healthcheck graceful (no infra) ---")
    from healthcheck import run_healthcheck

    run_healthcheck("smoke-test")
    log.info("  Healthcheck graceful ✅")


def test_merger_graceful() -> None:
    """Merger must return empty results gracefully without Redis."""
    log.info("--- Test: Merger graceful (no Redis) ---")
    from merger import get_macro_regime, merge

    snapshots = merge("15m", limit=20)
    macro = get_macro_regime()
    log.info("  Snapshots: %d, Macro: %s", len(snapshots), macro)
    log.info("  Merger graceful ✅")


def test_notifier_dry_run() -> None:
    """Build a mock embed and verify title/R:R without sending to Discord."""
    log.info("--- Test: Notifier embed dry-run ---")
    from models import PlaybookAlert
    from notifier_and_logger import compute_rr, format_embed

    mock = PlaybookAlert(
        symbol="NVDA",
        direction="LONG",
        edge_probability=0.82,
        confidence=0.85,
        timeframe="15m",
        thesis="Integration smoke test.",
        entry={"level": 875.0, "stop": 865.0, "target": 900.0},
        timeframe_rationale="Smoke test.",
        sentiment_context="N/A",
        unusual_activity=["smoke-test"],
        macro_regime="N/A",
        sources_agree=4,
    )
    embed = format_embed(mock)
    title = embed["embeds"][0]["title"]
    assert "NVDA" in title
    assert "HIGH EDGE" in title
    rr = compute_rr(mock.entry)
    assert rr != "N/A"
    log.info("  Title: %s", title)
    log.info("  R:R: %s", rr)
    log.info("  Notifier embed dry-run ✅")


def test_outcome_tracker_logic() -> None:
    """Run evaluate_outcome assertions (no live API or DB needed)."""
    log.info("--- Test: evaluate_outcome logic ---")
    from datetime import datetime, timedelta, timezone

    from outcome_tracker import evaluate_outcome

    base: dict = {
        "id": 1,
        "symbol": "SPY",
        "direction": "LONG",
        "entry_level": 500.0,
        "stop_level": 495.0,
        "target_level": 510.0,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "outcome": None,
    }
    assert evaluate_outcome(base, 511.0) == "WIN"
    assert evaluate_outcome(base, 494.0) == "LOSS"
    assert evaluate_outcome(base, 502.0) is None
    expired = {
        **base,
        "fired_at": datetime.now(timezone.utc) - timedelta(hours=5),
    }
    assert evaluate_outcome(expired, 502.0) == "EXPIRED"
    short = {
        **base,
        "direction": "SHORT",
        "stop_level": 505.0,
        "target_level": 490.0,
    }
    assert evaluate_outcome(short, 489.0) == "WIN"
    assert evaluate_outcome(short, 506.0) == "LOSS"
    log.info("  All evaluate_outcome assertions passed ✅")


def test_db_round_trip() -> None:
    """Insert, read-back, and update an alert (requires Postgres)."""
    if SKIP_INFRA or not os.getenv("DATABASE_URL"):
        log.info("--- Test: DB round-trip [SKIPPED — no infra] ---")
        return
    log.info("--- Test: DB round-trip ---")
    from db import get_recent_alerts, insert_alert, update_outcome
    from models import PlaybookAlert

    mock = PlaybookAlert(
        symbol="SMOKETEST",
        direction="LONG",
        edge_probability=0.80,
        confidence=0.82,
        timeframe="15m",
        thesis="Smoke test alert — safe to delete.",
        entry={"level": 100.0, "stop": 95.0, "target": 112.0},
        timeframe_rationale="Smoke test.",
        sentiment_context="N/A",
        unusual_activity=["smoke-test"],
        macro_regime="N/A",
        sources_agree=3,
    )
    alert_id = insert_alert(mock, raw_snapshots=[])
    log.info("  Inserted id=%d", alert_id)
    alerts = get_recent_alerts(limit=10)
    assert any(a["id"] == alert_id for a in alerts)
    update_outcome(alert_id, "WIN", 12.0)
    alerts = get_recent_alerts(limit=10)
    updated = next(a for a in alerts if a["id"] == alert_id)
    assert updated["outcome"] == "WIN"
    log.info("  DB round-trip ✅")


def test_docker_available() -> bool:
    """Check if Docker CLI is reachable."""
    log.info("--- Test: Docker availability ---")
    if check_docker():
        log.info("  Docker available ✅")
        return True
    log.warning("  Docker not installed — infra tests will be skipped ⚠️")
    return False


if __name__ == "__main__":
    log.info("=== trade-alert v0.8.0 Smoke Test ===\n")

    has_docker = test_docker_available()
    if not has_docker:
        SKIP_INFRA = True

    test_imports()
    test_file_inventory()
    test_healthcheck_graceful()
    test_merger_graceful()
    test_notifier_dry_run()
    test_outcome_tracker_logic()
    test_db_round_trip()

    log.info("\n=== All smoke tests passed ✅ ===")
