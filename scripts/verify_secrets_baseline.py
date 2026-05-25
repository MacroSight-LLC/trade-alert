#!/usr/bin/env python3
"""Fail CI only when new secret hashes appear, not on baseline line-number drift."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".secrets.baseline"


def _hashes(data: dict) -> set[str]:
    out: set[str] = set()
    for entries in data.get("results", {}).values():
        for entry in entries:
            out.add(entry["hashed_secret"])
    return out


def main() -> int:
    if not BASELINE.is_file():
        print(f"Missing baseline: {BASELINE}", file=sys.stderr)
        return 1

    before = json.loads(BASELINE.read_text(encoding="utf-8"))
    before_hashes = _hashes(before)

    subprocess.run(
        ["detect-secrets", "scan", "--update", str(BASELINE), str(ROOT)],
        check=True,
        cwd=ROOT,
    )

    after = json.loads(BASELINE.read_text(encoding="utf-8"))
    after_hashes = _hashes(after)

    new_hashes = after_hashes - before_hashes
    if new_hashes:
        print("New secrets detected (not in baseline):", file=sys.stderr)
        for h in sorted(new_hashes):
            print(f"  {h}", file=sys.stderr)
        print("Run: detect-secrets scan --update .secrets.baseline && detect-secrets audit .secrets.baseline", file=sys.stderr)
        return 1

    if before != after:
        # Line-number-only drift: restore committed baseline.
        BASELINE.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
        print("Baseline line numbers drifted; no new secrets (ignored).")

    print(f"Secrets baseline OK ({len(before_hashes)} allowlisted hashes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
