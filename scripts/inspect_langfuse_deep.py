"""Langfuse deep inspection via REST API (no SDK dependency).

Use this script when you need raw REST-level access to Langfuse data
(scores, datasets, cost analysis) without the Python SDK.

For SDK-based inspection, see ``inspect_langfuse_full.py`` instead.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("VAULT_ADDR", "http://vault:8200")
os.environ.setdefault("VAULT_TOKEN", "trade-alert-dev-token")

import httpx  # noqa: E402

host = os.getenv("LANGFUSE_HOST", "http://langfuse:3000")
pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
sk = os.getenv("LANGFUSE_SECRET_KEY", "")

client = httpx.Client(timeout=15.0, auth=(pk, sk))


def api(path: str, params: dict | None = None) -> dict:
    resp = client.get(f"{host}/api/public{path}", params=params or {})
    resp.raise_for_status()
    return resp.json()


# ── 1. SCORES ──
print("=" * 70)
print("1. ALL SCORES")
print("=" * 70)
try:
    scores = api("/scores", {"limit": "50"})
    score_data = scores.get("data", [])
    print(f"Total scores: {len(score_data)}")
    for s in score_data:
        print(f"  [{s.get('name')}] = {s.get('value')} — {s.get('comment', '')}")
        print(f"    trace: {s.get('traceId', '')[:12]}... | obs: {s.get('observationId', 'N/A')}")
except Exception as e:
    print(f"  Error: {e}")

# ── 2. DATASETS ──
print("\n" + "=" * 70)
print("2. DATASETS")
print("=" * 70)
try:
    datasets = api("/v2/datasets")
    ds_list = datasets.get("data", [])
    print(f"Total datasets: {len(ds_list)}")
    for ds in ds_list:
        print(f"\n  Dataset: {ds.get('name')}")
        print(f"  Description: {ds.get('description')}")
        print(f"  Created: {ds.get('createdAt')}")
        print(f"  Items count: {ds.get('items', '?')}")
        # Fetch items
        try:
            items = api("/v2/dataset-items", {"datasetName": ds.get("name"), "limit": "5"})
            item_list = items.get("data", [])
            print(f"  Fetched items: {len(item_list)}")
            for item in item_list[:3]:
                print(f"\n    Item ID: {item.get('id', '')[:16]}...")
                print(f"    Status: {item.get('status')}")
                inp = item.get("input", {})
                if isinstance(inp, dict):
                    print(f"    Input keys: {list(inp.keys())}")
                    for k, v in inp.items():
                        v_str = json.dumps(v, default=str) if not isinstance(v, str) else v
                        print(f"      {k}: {v_str[:150]}{'...' if len(v_str) > 150 else ''}")
                exp = item.get("expectedOutput", {})
                if isinstance(exp, dict):
                    print(f"    Expected output keys: {list(exp.keys())}")
                    for k, v in exp.items():
                        v_str = json.dumps(v, default=str) if not isinstance(v, str) else v
                        print(f"      {k}: {v_str[:150]}{'...' if len(v_str) > 150 else ''}")
                meta = item.get("metadata", {})
                if meta:
                    print(f"    Metadata: {json.dumps(meta, default=str)[:300]}")
        except Exception as e:
            print(f"  Items error: {e}")
except Exception as e:
    print(f"  Error: {e}")

# ── 3. TRACE DETAIL (observations, timing, cost) ──
print("\n" + "=" * 70)
print("3. TRACE DETAILS (all observations)")
print("=" * 70)
try:
    traces = api("/traces", {"limit": "5"})
    for t in traces.get("data", []):
        tid = t.get("id")
        print(f"\n  Trace: {tid}")
        print(f"  Name: {t.get('name')} | Session: {t.get('sessionId')}")
        print(f"  Tags: {t.get('tags')}")

        # Compute total cost from observations
        obs = api("/observations", {"traceId": tid, "limit": "50"})
        obs_list = obs.get("data", [])
        total_cost = 0.0
        total_tokens = 0

        for ob in obs_list:
            name = ob.get("name", "?")
            ob_type = ob.get("type", "?")
            model = ob.get("model", "")
            usage = ob.get("usage", {}) or {}
            cost = ob.get("calculatedTotalCost") or ob.get("totalCost") or 0
            total_cost += float(cost) if cost else 0
            tokens = usage.get("total", 0) or 0
            total_tokens += tokens

            start = ob.get("startTime", "")
            end = ob.get("endTime", "")

            level = ob.get("level", "")
            status = ob.get("statusMessage", "")

            line = f"    {ob_type:10s} {name:30s}"
            if model:
                line += f" model={model}"
            if tokens:
                line += f" tokens={tokens}"
            if cost:
                line += f" cost=${float(cost):.4f}"
            if level:
                line += f" level={level}"
            print(line)

        print(f"  TOTALS: {total_tokens} tokens, ${total_cost:.4f} cost")

        # Get scores for this trace
        try:
            scores = api("/scores", {"traceId": tid, "limit": "50"})
            score_list = scores.get("data", [])
            if score_list:
                print(f"  SCORES ({len(score_list)}):")
                for s in score_list:
                    print(f"    {s.get('name')}: {s.get('value')} — {s.get('comment', '')}")
            else:
                print("  SCORES: none")
        except Exception as e:
            print(f"  Scores error: {e}")
except Exception as e:
    print(f"  Error: {e}")

# ── 4. SESSION ANALYSIS ──
print("\n" + "=" * 70)
print("4. SESSION ANALYSIS")
print("=" * 70)
try:
    sessions = api("/sessions", {"limit": "10"})
    for s in sessions.get("data", []):
        sid = s.get("id")
        print(f"\n  Session: {sid}")
        print(f"  Created: {s.get('createdAt')}")
        # Count traces in session
        session_traces = api("/traces", {"sessionId": sid, "limit": "50"})
        trace_count = len(session_traces.get("data", []))
        print(f"  Traces: {trace_count}")
except Exception as e:
    print(f"  Error: {e}")

# ── 5. MODELS / COST SUMMARY ──
print("\n" + "=" * 70)
print("5. MODEL USAGE SUMMARY")
print("=" * 70)
try:
    obs_all = api("/observations", {"type": "GENERATION", "limit": "50"})
    model_stats: dict[str, dict] = {}
    for ob in obs_all.get("data", []):
        model = ob.get("model", "unknown")
        usage = ob.get("usage", {}) or {}
        cost = float(ob.get("calculatedTotalCost") or ob.get("totalCost") or 0)
        tokens_in = usage.get("input", 0) or 0
        tokens_out = usage.get("output", 0) or 0

        if model not in model_stats:
            model_stats[model] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        model_stats[model]["calls"] += 1
        model_stats[model]["input_tokens"] += tokens_in
        model_stats[model]["output_tokens"] += tokens_out
        model_stats[model]["cost"] += cost

    for model, stats in model_stats.items():
        print(f"\n  Model: {model}")
        print(f"    Calls: {stats['calls']}")
        print(f"    Input tokens: {stats['input_tokens']:,}")
        print(f"    Output tokens: {stats['output_tokens']:,}")
        print(f"    Total cost: ${stats['cost']:.4f}")
        if stats['calls'] > 0:
            print(
                f"    Avg tokens/call: {(stats['input_tokens'] + stats['output_tokens']) // stats['calls']:,}"
            )
            print(f"    Avg cost/call: ${stats['cost'] / stats['calls']:.4f}")
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 70)
print("DEEP INSPECTION COMPLETE")
print("=" * 70)
