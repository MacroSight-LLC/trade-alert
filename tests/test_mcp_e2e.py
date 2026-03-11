"""End-to-end MCP pipeline smoke test.

Runs inside the app container to verify:
  MCP calls → normalizers → Redis snapshots → merger
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

# Ensure repo root is on sys.path when running from tests/ subdirectory.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import redis

from normalizers.flow_normalizer import normalize as flow_normalize
from normalizers.macro_normalizer import normalize as macro_normalize
from normalizers.ta_normalizer import normalize as ta_normalize

r = redis.Redis(host="redis", port=6379, decode_responses=True)
TTL = 900

# Flush old snapshots for clean test
r.delete("snapshots:15m", "snapshots:1h", "universe:equities", "universe:crypto", "macro:regime")


def mcp_get(host: str, port: int, tool: str) -> dict | list:
    resp = urllib.request.urlopen(f"http://{host}:{port}/tool/{tool}", timeout=5)
    return json.loads(resp.read())


# 1. Build universes
cg = mcp_get("coingecko-mcp", 8007, "top_gainers")
screen = mcp_get("trading-mcp", 8008, "screen")
crypto = [item["symbol"] for item in cg]
equities = [item["symbol"] for item in screen.get("results", [])]
r.setex("universe:equities", TTL, json.dumps(equities))
r.setex("universe:crypto", TTL, json.dumps(crypto))
print(f"1. Universes: {len(equities)} equities, {len(crypto)} crypto")

# 2. TA signals
ta_bb = mcp_get("tradingview-mcp", 8001, "bollinger_scan")
ta_rsi = mcp_get("tradingview-mcp", 8001, "rsi_scan")
raw_ta: dict = {}
for item in ta_bb.get("results", []):
    sym = item["symbol"]
    raw_ta[sym] = {
        "rating": 2.0 if item.get("squeeze") else 1.0,
        "patterns": ["bb_squeeze"] if item.get("squeeze") else [],
        "indicators": {"bb_squeeze": item.get("squeeze", False)},
    }
for item in ta_rsi.get("results", []):
    sym = item["symbol"]
    rsi = item.get("rsi", 50)
    if sym not in raw_ta:
        raw_ta[sym] = {"rating": None, "patterns": [], "indicators": {}}
    if rsi < 30:
        raw_ta[sym]["rating"] = -2.0
    elif rsi > 70:
        raw_ta[sym]["rating"] = 2.0
    raw_ta[sym]["indicators"]["rsi"] = rsi

ta_snaps = ta_normalize(raw_ta, timeframe="15m")
for snap in ta_snaps:
    r.lpush("snapshots:15m", snap.model_dump_json())
    r.expire("snapshots:15m", TTL)
print(f"2. TA: {len(ta_snaps)} snapshots")

# 3. Flow signals
aggs = mcp_get("polygon-mcp", 8002, "aggs")
raw_flow: dict = {}
for item in aggs.get("results", []):
    sym = item["symbol"]
    vol = item.get("volume", 0)
    avg = item.get("avg_volume", 1) or 1
    raw_flow[sym] = {"volume_multiple": vol / avg}
flow_snaps = flow_normalize(raw_flow, timeframe="15m")
for snap in flow_snaps:
    r.lpush("snapshots:15m", snap.model_dump_json())
    r.expire("snapshots:15m", TTL)
print(f"3. Flow: {len(flow_snaps)} snapshots")

# 4. Macro signals
vix = mcp_get("fred-mcp", 8009, "vix_level")
yc = mcp_get("fred-mcp", 8009, "yield_curve")
macro_data = {
    "vix": vix.get("vix_level", vix.get("value")),
    "yield_curve_slope": yc.get("spread_bps", yc.get("value")),
    "risk_on": True,
}
r.setex("macro:regime", TTL, json.dumps(macro_data))
macro_snaps = macro_normalize(macro_data, timeframe="15m")
for snap in macro_snaps:
    r.lpush("snapshots:15m", snap.model_dump_json())
    r.expire("snapshots:15m", TTL)
print(f"4. Macro: VIX={macro_data['vix']}, risk_on={macro_data['risk_on']}, {len(macro_snaps)} snapshots")

# 5. Sentiment signals
sent = mcp_get("finnhub-mcp", 8004, "sentiment")
from normalizers.sentiment_normalizer import normalize as sent_normalize

raw_sent: dict = {}
for item in sent:
    sym = item.get("symbol")
    if sym:
        raw_sent[sym] = {"finnhub_score": item.get("score", 0.0), "spam_filtered": False}
sent_snaps = sent_normalize(raw_sent, timeframe="15m")
for snap in sent_snaps:
    r.lpush("snapshots:15m", snap.model_dump_json())
    r.expire("snapshots:15m", TTL)
print(f"5. Sentiment: {len(sent_snaps)} snapshots")

# 6. Merger
from merger import get_macro_regime, merge

candidates = merge("15m", limit=20)
macro = get_macro_regime()
print(f"6. Merger: {len(candidates)} candidates")
for snap in candidates:
    sigs = ", ".join(f"{s.source}:{s.type}({s.score:+.1f})" for s in snap.signals)
    print(f"   {snap.symbol}: {sigs}")

total = len(ta_snaps) + len(flow_snaps) + len(macro_snaps) + len(sent_snaps)
print(f"\n=== Pipeline complete: {total} snapshots → {len(candidates)} candidates ===")
