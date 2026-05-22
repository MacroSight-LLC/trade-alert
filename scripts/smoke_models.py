#!/usr/bin/env python3
"""Smoke-construct every Pydantic model in models.py with sample data.

Used as a quick interactive sanity check that all validators accept a
realistic, well-formed payload. Run from the repo root::

    python scripts/smoke_models.py

This script intentionally lives outside ``models.py`` so the module stays
schema-only and AI tools that read it for guidance aren't tempted to copy
the demo prices/version strings as canonical values.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from models import PlaybookAlert, Signal, Snapshot, TraceAnalysis  # noqa: E402


def main() -> None:
    """Construct one of each model with sample data and pretty-print results."""
    s = Signal(
        source="test",
        type="technical_trend",
        score=1.5,
        confidence=0.8,
        reason="BB squeeze detected",
    )
    snap = Snapshot(
        symbol="AAPL",
        timeframe="15m",
        timestamp=datetime.now(UTC),
        signals=[s],
    )
    alert = PlaybookAlert(
        symbol="AAPL",
        direction="LONG",
        edge_probability=0.75,
        confidence=0.80,
        timeframe="15m",
        thesis="Bollinger Band squeeze with volume confirmation.",
        entry={"level": 185.0, "stop": 182.0, "target": 192.0},
        timeframe_rationale="15m trend aligning with 1h structure.",
        sentiment_context="Retail bullish, institutional neutral.",
        unusual_activity=["IV spike 2x avg", "options sweep $190c"],
        macro_regime="Risk-on, VIX 14, curve normal.",
        sources_agree=4,
    )
    trace = TraceAnalysis(
        trace_id="lf-abc-123",
        is_healthy=True,
        cost_usd=0.012,
        latency_s=4.1,
        llm_calls=2,
        total_tokens=8421,
        prompt_version="decision-v3",
        timestamp=datetime.now(UTC),
    )
    print("Signal:", s.model_dump())
    print("Snapshot:", snap.model_dump())
    print("Alert:", alert.model_dump())
    print("TraceAnalysis:", trace.model_dump())
    print("All models valid.")


if __name__ == "__main__":
    main()
