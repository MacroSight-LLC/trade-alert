"""Inspect exact prompt content sent to the LLM in the latest traces."""

from __future__ import annotations

import os

os.environ.setdefault("VAULT_ADDR", "http://vault:8200")
os.environ.setdefault("VAULT_TOKEN", "trade-alert-dev-token")

import httpx  # noqa: E402

host = os.getenv("LANGFUSE_HOST", "http://langfuse:3000")
pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
sk = os.getenv("LANGFUSE_SECRET_KEY", "")

client = httpx.Client(timeout=15.0, auth=(pk, sk))

# Get generations
resp = client.get(f"{host}/api/public/observations", params={"type": "GENERATION", "limit": "2"})
resp.raise_for_status()
gens = resp.json().get("data", [])

for g in gens:
    trace = g.get("traceId", "?")[:12]
    print(f"\n{'=' * 70}")
    print(f"Generation: {g.get('name')} (trace: {trace}...)")
    print(f"Model: {g.get('model')}")

    inp = g.get("input", {})
    if isinstance(inp, dict) and "messages" in inp:
        for msg in inp["messages"]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            print(f"\n--- {role.upper()} prompt ({len(content)} chars) ---")
            print(content[:6000])
            if len(content) > 6000:
                print(f"\n... truncated ({len(content)} total chars)")

    out = g.get("output", {})
    if isinstance(out, dict):
        content = out.get("content", "")
    else:
        content = str(out)
    print("\n--- OUTPUT ---")
    print(content[:500])

    # Usage
    usage = g.get("usage", {})
    cost = g.get("calculatedTotalCost") or g.get("totalCost")
    print("\n--- USAGE ---")
    print(f"Input tokens: {usage.get('input', '?')}")
    print(f"Output tokens: {usage.get('output', '?')}")
    print(f"Cost: ${float(cost):.4f}" if cost else "Cost: N/A")
