#!/usr/bin/env python3
"""List alerts in a local pg_dump for cherry-picking into legacy_alerts.

Usage:
  python scripts/list_dump_alerts.py
  python scripts/list_dump_alerts.py /path/to/trade_alert.dump

Reads from Docker volume trade-alert_pg-backups by default (Apr 2026 backups).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DUMP = "trade_alert_20260410_030000.dump"
DEFAULT_VOLUME = "trade-alert_pg-backups"


def _iter_copy_rows(dump_path: Path) -> list[str]:
    proc = subprocess.run(
        ["pg_restore", "-f", "-", str(dump_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "pg_restore failed")
    rows: list[str] = []
    in_copy = False
    for line in proc.stdout.splitlines():
        if line.startswith("COPY public.alerts "):
            in_copy = True
            continue
        if in_copy and line == "\\.":
            break
        if in_copy:
            rows.append(line)
    return rows


def _field(row: str, idx: int) -> str:
    parts = row.split("\t")
    if idx >= len(parts):
        return ""
    val = parts[idx]
    return "" if val == r"\N" else val


def main() -> int:
    parser = argparse.ArgumentParser(description="List alerts in a pg_dump")
    parser.add_argument("dump", nargs="?", help="Path to .dump file (or use --from-docker-volume)")
    parser.add_argument(
        "--from-docker-volume",
        action="store_true",
        help=f"Read {DEFAULT_DUMP} from Docker volume {DEFAULT_VOLUME}",
    )
    args = parser.parse_args()

    dump_path: Path | None = None
    cleanup: Path | None = None
    if args.from_docker_volume or not args.dump:
        tmp = Path("/tmp") / DEFAULT_DUMP
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{DEFAULT_VOLUME}:/backups:ro",
                "-v",
                f"{tmp.parent}:/out",
                "alpine",
                "cp",
                f"/backups/{DEFAULT_DUMP}",
                f"/out/{DEFAULT_DUMP}",
            ],
            check=True,
        )
        dump_path = tmp
    else:
        dump_path = Path(args.dump)

    rows = _iter_copy_rows(dump_path)
    print(f"{'ID':>4}  {'SYM':<6} {'DIR':<6} {'EP':>5} {'TF':<4}  {'OUT':<8}  CREATED")
    print("-" * 72)
    for row in rows:
        aid = _field(row, 0)
        sym = _field(row, 1)
        direction = _field(row, 2)
        ep = _field(row, 3)
        tf = _field(row, 5)
        outcome = _field(row, 16) or "OPEN"
        created = _field(row, 14)[:19]
        print(f"{aid:>4}  {sym:<6} {direction:<6} {ep:>5} {tf:<4}  {outcome:<8}  {created}")
    print(f"\n{len(rows)} alert(s). Add IDs to legacy/manifest.json, then run import_legacy_alerts.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
