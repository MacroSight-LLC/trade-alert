#!/usr/bin/env python3
"""Verify orchestrator-15m and orchestrator-1h wrappers stay in sync."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"
WRAPPERS = ("orchestrator-15m.yaml", "orchestrator-1h.yaml")
REQUIRED_INPUTS = frozenset(
    {
        "timeframe",
        "snapshot_key",
        "decision_workflow",
        "schedule",
        "trace_label",
        "failure_message",
    }
)


def _load_wrapper(name: str) -> dict:
    path = WORKFLOWS / name
    data = yaml.safe_load(path.read_text())
    steps = data.get("steps") or []
    if len(steps) != 1 or steps[0].get("type") != "workflow":
        raise ValueError(f"{name}: expected single type: workflow step")
    if steps[0].get("workflow") != "orchestrator-base.yaml":
        raise ValueError(f"{name}: must delegate to orchestrator-base.yaml")
    return steps[0].get("inputs") or {}


def main() -> int:
    inputs_by_file = {name: _load_wrapper(name) for name in WRAPPERS}
    keys_15m = set(inputs_by_file["orchestrator-15m.yaml"])
    keys_1h = set(inputs_by_file["orchestrator-1h.yaml"])
    if keys_15m != keys_1h:
        print(f"FAIL: input key mismatch {keys_15m ^ keys_1h}")
        return 1
    missing = REQUIRED_INPUTS - keys_15m
    if missing:
        print(f"FAIL: missing required inputs: {sorted(missing)}")
        return 1
    if not (WORKFLOWS / "orchestrator-base.yaml").exists():
        print("FAIL: orchestrator-base.yaml missing")
        return 1
    print("OK: orchestrator wrappers in parity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
