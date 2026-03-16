"""E2E test: push a realistic alert through the full notification pipeline.

Tests: PlaybookAlert validation → chart generation → Postgres insert → Discord embed with chart.
"""

from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("VAULT_ADDR", "http://vault:8200")
os.environ.setdefault("VAULT_TOKEN", "trade-alert-dev-token")

from notifier_and_logger import notify  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Realistic test alert — NVDA LONG setup with multi-source confluence
test_alerts = [
    {
        "symbol": "NVDA",
        "direction": "LONG",
        "edge_probability": 0.82,
        "confidence": 0.85,
        "timeframe": "15m",
        "thesis": (
            "Bollinger Band squeeze resolving upward with 2.8x average volume, "
            "unusual call sweep activity ($145c weeklies, 4000 contracts), "
            "positive retail sentiment shift on ROT. Classic breakout pattern "
            "with institutional confirmation via dark pool prints above VWAP."
        ),
        "entry": {"level": 136.50, "stop": 133.80, "target": 142.00},
        "timeframe_rationale": ("15m breakout aligning with 1h uptrend structure and daily support bounce."),
        "sentiment_context": (
            "ROT strong_bullish, Finnhub aggregate +0.72. Social media chatter elevated but quality-filtered."
        ),
        "unusual_activity": [
            "Call sweep: $145c weeklies, 4000 contracts @ $2.15",
            "Dark pool print: 180K shares above VWAP",
            "IV rank rising to 62nd percentile",
        ],
        "macro_regime": "Risk-on rotation. VIX 18.4, yield curve +42bps. Tech sector leading.",
        "sources_agree": 5,
    }
]

alerts_json = json.dumps(test_alerts)

print("=" * 60)
print("E2E NOTIFICATION TEST")
print("=" * 60)
print(f"Alert: {test_alerts[0]['symbol']} {test_alerts[0]['direction']}")
print(f"Entry: ${test_alerts[0]['entry']['level']}")
print(f"Stop:  ${test_alerts[0]['entry']['stop']}")
print(f"Target: ${test_alerts[0]['entry']['target']}")
print("=" * 60)

n_sent = notify(alerts_json=alerts_json, raw_snapshots=[])

print("=" * 60)
print(f"RESULT: {n_sent} alert(s) sent to Discord")
if n_sent > 0:
    print("SUCCESS: Full E2E notification pipeline verified!")
else:
    print("WARNING: Alert was not sent — check logs above")
print("=" * 60)
