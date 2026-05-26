#!/usr/bin/env python3
"""Import cherry-picked alerts from a pg_dump into legacy_alerts (not prod alerts).

Usage:
  # List candidates first
  python scripts/list_dump_alerts.py --from-docker-volume

  # Import manifest IDs (local Docker Postgres)
  export DATABASE_URL=postgresql://trade_alert:...@localhost:5432/trade_alert
  python scripts/import_legacy_alerts.py --from-docker-volume

  # Hetzner (after applying schema + scp dump or use --from-docker-volume on laptop
  # with SSH tunnel to prod Postgres)

Requires: pg_restore on PATH, legacy/manifest.json, DATABASE_URL or POSTGRES_* env.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import psycopg2
except ImportError:
    print("ERROR: pip install psycopg2-binary", file=sys.stderr)
    raise SystemExit(1) from None

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SQL = REPO_ROOT / "scripts" / "migrate_legacy_alerts.sql"

DEFAULT_DUMP = "trade_alert_20260410_030000.dump"
DEFAULT_VOLUME = "trade-alert_pg-backups"
MANIFEST = REPO_ROOT / "legacy" / "manifest.json"

# Column order in pg_dump COPY for alerts (Apr 2026 backups)
_COLS = [
    "id",
    "symbol",
    "direction",
    "edge_probability",
    "confidence",
    "timeframe",
    "thesis",
    "entry",
    "timeframe_rationale",
    "sentiment_context",
    "unusual_activity",
    "macro_regime",
    "sources_agree",
    "raw_snapshots",
    "created_at",
    "updated_at",
    "outcome",
    "outcome_pnl",
    "outcome_pnl_pct",
    "forecast_score",
    "forecast_contradicted",
    "langfuse_trace_id",
]


def _parse_field(raw: str) -> object | None:
    if raw == r"\N":
        return None
    return raw


def _row_to_dict(line: str) -> dict:
    parts = line.split("\t")
    data: dict = {}
    for i, col in enumerate(_COLS):
        if i >= len(parts):
            data[col] = None
            continue
        val = _parse_field(parts[i])
        if col == "entry" and isinstance(val, str):
            data[col] = json.loads(val)
        elif col in ("unusual_activity", "raw_snapshots") and isinstance(val, str):
            try:
                data[col] = json.loads(val) if val.startswith("[") or val.startswith("{") else val
            except json.JSONDecodeError:
                data[col] = val
        elif col in ("edge_probability", "confidence", "outcome_pnl", "outcome_pnl_pct", "forecast_score"):
            data[col] = float(val) if val is not None else None
        elif col == "sources_agree":
            data[col] = int(val) if val is not None else None
        elif col == "forecast_contradicted":
            data[col] = str(val).lower() in ("t", "true", "1")
        else:
            data[col] = val
    return data


def _iter_copy_rows(dump_path: Path) -> dict[int, dict]:
    proc = subprocess.run(
        ["pg_restore", "-f", "-", str(dump_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or "pg_restore failed")
    by_id: dict[int, dict] = {}
    in_copy = False
    for line in proc.stdout.splitlines():
        if line.startswith("COPY public.alerts "):
            in_copy = True
            continue
        if in_copy and line == "\\.":
            break
        if in_copy:
            row = _row_to_dict(line)
            by_id[int(row["id"])] = row
    return by_id


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: set DATABASE_URL", file=sys.stderr)
        raise SystemExit(1)
    return psycopg2.connect(url)


def _ensure_schema(conn) -> None:
    sql = MIGRATE_SQL.read_text()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _legacy_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM legacy_alerts")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _insert_legacy(conn, row: dict) -> int:
    entry = row.get("entry")
    if isinstance(entry, dict):
        entry = json.dumps(entry)
    unusual = row.get("unusual_activity")
    if unusual is None:
        unusual = json.dumps([])
    elif isinstance(unusual, (list, dict)):
        unusual = json.dumps(unusual)
    snapshots = row.get("raw_snapshots")
    if snapshots is None:
        snapshots = json.dumps([])
    elif isinstance(snapshots, (list, dict)):
        snapshots = json.dumps(snapshots)

    sql = """
        INSERT INTO legacy_alerts (
            symbol, direction, edge_probability, confidence, timeframe,
            thesis, entry, timeframe_rationale, sentiment_context,
            unusual_activity, macro_regime, sources_agree, raw_snapshots,
            created_at, updated_at, outcome, outcome_pnl, outcome_pnl_pct,
            forecast_score, forecast_contradicted, langfuse_trace_id,
            source_alert_id, legacy_note, source_dump
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s::jsonb, %s, %s,
            %s::jsonb, %s, %s, %s::jsonb,
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (source_alert_id, source_dump) DO NOTHING
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                row["symbol"],
                row["direction"],
                row["edge_probability"],
                row["confidence"],
                row["timeframe"],
                row["thesis"],
                entry,
                row.get("timeframe_rationale"),
                row.get("sentiment_context"),
                unusual,
                row.get("macro_regime"),
                row.get("sources_agree"),
                snapshots,
                row.get("created_at"),
                row.get("updated_at") or row.get("created_at"),
                row.get("outcome"),
                row.get("outcome_pnl"),
                row.get("outcome_pnl_pct"),
                row.get("forecast_score"),
                row.get("forecast_contradicted", False),
                row.get("langfuse_trace_id"),
                row["source_alert_id"],
                row.get("legacy_note"),
                row.get("source_dump"),
            ),
        )
        result = cur.fetchone()
        conn.commit()
        return int(result[0]) if result else 0


def _resolve_dump_path(args: argparse.Namespace) -> Path:
    if args.dump:
        return Path(args.dump)
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
    return tmp


def main() -> int:
    parser = argparse.ArgumentParser(description="Import cherry-picked legacy alerts")
    parser.add_argument("--dump", help="Path to pg_dump file")
    parser.add_argument("--from-docker-volume", action="store_true", help="Use trade-alert_pg-backups volume")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    ids: list[int] = manifest["alert_ids"]
    notes: dict[str, str] = manifest.get("notes", {})
    source_dump = manifest.get("source_dump", DEFAULT_DUMP)

    dump_path = _resolve_dump_path(args) if (args.from_docker_volume or not args.dump) else Path(args.dump)
    rows_by_id = _iter_copy_rows(dump_path)

    missing = [i for i in ids if i not in rows_by_id]
    if missing:
        print(f"ERROR: IDs not in dump: {missing}", file=sys.stderr)
        return 1

    if args.dry_run:
        for aid in ids:
            r = rows_by_id[aid]
            print(f"  would import #{aid} {r['symbol']} {r['direction']} EP={r['edge_probability']}")
        return 0

    conn = _connect()
    _ensure_schema(conn)
    before = _legacy_count(conn)
    imported = 0
    for aid in ids:
        row = rows_by_id[aid]
        row["source_alert_id"] = aid
        row["source_dump"] = source_dump
        row["legacy_note"] = notes.get(str(aid), "")
        new_id = _insert_legacy(conn, row)
        if new_id:
            imported += 1
            print(f"  imported #{aid} -> legacy_alerts.id={new_id} ({row['symbol']} {row['direction']})")
        else:
            print(f"  skipped #{aid} (already imported)")

    after = _legacy_count(conn)
    conn.close()
    print(f"\nDone: {imported} new row(s); legacy_alerts total {after} (was {before})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
