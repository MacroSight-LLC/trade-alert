"""Inspect the most recent Langfuse trace to see LLM raw output."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VAULT_ADDR", "http://vault:8200")
os.environ.setdefault("VAULT_TOKEN", "trade-alert-dev-token")

from langfuse_client import get_langfuse_client  # noqa: E402

lf = get_langfuse_client()
traces = lf.fetch_traces(limit=1)
if not traces.data:
    print("No traces found")
    raise SystemExit

t = traces.data[0]
print(f"Trace: {t.id}")
print(f"Name: {t.name}")
print(f"Tags: {t.tags}")

obs = lf.fetch_observations(trace_id=t.id, type="GENERATION")
for o in obs.data:
    print(f"\n=== Generation: {o.name} ===")
    print(f"Model: {o.model}")
    if o.usage:
        print(f"Input tokens: {o.usage.input}, Output tokens: {o.usage.output}")
    # Show input (the prompt)
    inp = o.input
    if isinstance(inp, (dict, list)):
        inp = json.dumps(inp, indent=2)
    print(f"Input prompt (first 5000 chars):\n{str(inp)[:5000]}")
    out = o.output
    if isinstance(out, dict):
        out = json.dumps(out, indent=2)
    print(f"\nOutput:\n{str(out)[:3000]}")

# Also show scores
scores = lf.fetch_scores(trace_id=t.id) if hasattr(lf, "fetch_scores") else None
if scores and hasattr(scores, "data"):
    print("\n=== Scores ===")
    for s in scores.data:
        print(f"  {s.name}: {s.value} ({s.comment})")
