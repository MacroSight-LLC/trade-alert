"""Full Langfuse inspection: traces, scores, generations, datasets, prompts."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VAULT_ADDR", "http://vault:8200")
os.environ.setdefault("VAULT_TOKEN", "trade-alert-dev-token")

from langfuse_client import get_langfuse_client  # noqa: E402

lf = get_langfuse_client()
if not lf:
    print("ERROR: Langfuse client not available")
    raise SystemExit(1)

print("=" * 70)
print("LANGFUSE FULL INSPECTION")
print("=" * 70)

# ── 1. Traces ──
print("\n" + "─" * 70)
print("1. TRACES")
print("─" * 70)
traces = lf.fetch_traces(limit=10)
for t in traces.data:
    print(f"\n  Trace: {t.id}")
    print(f"  Name: {t.name}")
    print(f"  Session: {t.session_id}")
    print(f"  Tags: {t.tags}")
    print(f"  Created: {t.timestamp}")
    if hasattr(t, "metadata") and t.metadata:
        print(f"  Metadata: {json.dumps(t.metadata, indent=4, default=str)[:500]}")

# ── 2. Scores for each trace ──
print("\n" + "─" * 70)
print("2. SCORES (per trace)")
print("─" * 70)
for t in traces.data:
    print(f"\n  Trace: {t.id[:12]}... ({t.name})")
    try:
        # Try fetching scores via the observations
        obs_all = lf.fetch_observations(trace_id=t.id)
        for ob in obs_all.data:
            if hasattr(ob, "scores") and ob.scores:
                for sc in ob.scores:
                    print(f"    [{ob.name}] {sc.name}: {sc.value} — {sc.comment}")
    except Exception as e:
        print(f"    (score fetch via obs failed: {e})")

    # Also try trace-level scores via API
    try:
        scores_resp = lf.fetch_scores(page=1, limit=50)
        trace_scores = [s for s in scores_resp.data if s.trace_id == t.id]
        if trace_scores:
            for s in trace_scores:
                print(f"    [trace] {s.name}: {s.value} — {s.comment}")
        else:
            print("    (no trace-level scores found)")
    except Exception as e:
        print(f"    (trace scores API: {e})")

# ── 3. Generations (LLM calls) ──
print("\n" + "─" * 70)
print("3. GENERATIONS (LLM calls)")
print("─" * 70)
for t in traces.data:
    gens = lf.fetch_observations(trace_id=t.id, type="GENERATION")
    for g in gens.data:
        print(f"\n  Trace: {t.id[:12]}... → Generation: {g.name}")
        print(f"  Model: {g.model}")
        if g.usage:
            print(f"  Tokens — Input: {g.usage.input}, Output: {g.usage.output}, Total: {g.usage.total}")
            if hasattr(g.usage, "total_cost") and g.usage.total_cost:
                print(f"  Cost: ${g.usage.total_cost:.6f}")
        print(f"  Latency: {g.latency}ms" if hasattr(g, "latency") and g.latency else "")
        print(f"  Start: {g.start_time}")
        print(f"  End: {g.end_time}")
        # Show output
        out = g.output
        if isinstance(out, dict):
            content = out.get("content", "")
            print(f"  Output content: {str(content)[:300]}")
        else:
            print(f"  Output: {str(out)[:300]}")
        # Show input messages summary
        inp = g.input
        if isinstance(inp, dict) and "messages" in inp:
            msgs = inp["messages"]
            for m in msgs:
                role = m.get("role", "?")
                content = m.get("content", "")
                print(f"  Input [{role}]: {len(content)} chars")

# ── 4. All Observations (spans, events) ──
print("\n" + "─" * 70)
print("4. OBSERVATION TYPES (spans & events per trace)")
print("─" * 70)
for t in traces.data:
    obs_all = lf.fetch_observations(trace_id=t.id)
    type_counts: dict[str, int] = {}
    span_details: list[str] = []
    for ob in obs_all.data:
        tp = ob.type or "UNKNOWN"
        type_counts[tp] = type_counts.get(tp, 0) + 1
        if tp == "SPAN":
            duration = ""
            if ob.start_time and ob.end_time:
                try:
                    dt = (ob.end_time - ob.start_time).total_seconds()
                    duration = f" ({dt:.2f}s)"
                except Exception:
                    pass
            span_details.append(f"    SPAN: {ob.name}{duration}")
        elif tp == "EVENT":
            span_details.append(f"    EVENT: {ob.name}")
    print(f"\n  Trace: {t.id[:12]}... ({t.name})")
    print(f"  Observation counts: {type_counts}")
    for sd in span_details[:20]:
        print(sd)
    if len(span_details) > 20:
        print(f"    ... and {len(span_details) - 20} more")

# ── 5. Datasets ──
print("\n" + "─" * 70)
print("5. DATASETS")
print("─" * 70)
try:
    datasets = lf.fetch_datasets()
    if hasattr(datasets, "data"):
        for ds in datasets.data:
            print(f"\n  Dataset: {ds.name}")
            print(f"  Description: {ds.description}")
            print(f"  Created: {ds.created_at}")
            # Fetch items
            try:
                items = lf.fetch_dataset_items(dataset_name=ds.name, limit=5)
                if hasattr(items, "data"):
                    print(f"  Items: {len(items.data)}")
                    for item in items.data[:3]:
                        print(f"    Item: {item.id[:12]}...")
                        if item.input:
                            inp_str = json.dumps(item.input, default=str)
                            print(
                                f"      Input keys: {list(item.input.keys()) if isinstance(item.input, dict) else type(item.input).__name__}"
                            )
                            print(f"      Input size: {len(inp_str)} chars")
                        if item.expected_output:
                            out_str = json.dumps(item.expected_output, default=str)
                            print(
                                f"      Expected output keys: {list(item.expected_output.keys()) if isinstance(item.expected_output, dict) else type(item.expected_output).__name__}"
                            )
                            print(f"      Expected output size: {len(out_str)} chars")
                        if item.metadata:
                            print(f"      Metadata: {json.dumps(item.metadata, default=str)[:200]}")
            except Exception as e:
                print(f"  Items fetch failed: {e}")
    else:
        print("  No datasets found")
except Exception as e:
    print(f"  Dataset fetch failed: {e}")

# ── 6. Prompts ──
print("\n" + "─" * 70)
print("6. PROMPTS (registered in Langfuse)")
print("─" * 70)
try:
    # Try listing prompts (API may vary by version)
    import httpx

    host = os.getenv("LANGFUSE_HOST", "http://langfuse:3000")
    pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    sk = os.getenv("LANGFUSE_SECRET_KEY", "")
    resp = httpx.get(
        f"{host}/api/public/v2/prompts",
        auth=(pk, sk),
        timeout=10.0,
    )
    if resp.status_code == 200:
        data = resp.json()
        prompts = data.get("data", data.get("prompts", []))
        if prompts:
            for p in prompts:
                print(f"  Prompt: {p.get('name')} (version {p.get('version')})")
                print(f"    Labels: {p.get('labels')}")
                print(f"    Type: {p.get('type')}")
        else:
            print("  No prompts registered in Langfuse")
    else:
        print(f"  Prompt API: {resp.status_code} — {resp.text[:200]}")
except Exception as e:
    print(f"  Prompt listing failed: {e}")

# ── 7. Sessions ──
print("\n" + "─" * 70)
print("7. SESSIONS")
print("─" * 70)
try:
    sessions = lf.fetch_sessions(limit=10)
    if hasattr(sessions, "data"):
        for s in sessions.data:
            print(f"  Session: {s.id}")
            print(f"    Created: {s.created_at}")
            if hasattr(s, "traces") and s.traces:
                print(f"    Traces: {len(s.traces)}")
    else:
        print("  No sessions found")
except Exception as e:
    print(f"  Session fetch: {e}")

print("\n" + "=" * 70)
print("INSPECTION COMPLETE")
print("=" * 70)
