#!/usr/bin/env python3
"""Delete alerts older than DATA_RETENTION_DAYS (default 180)."""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "180"))
PURGE_VACUUM_ENABLED = os.environ.get("PURGE_VACUUM_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)


def purge_old_alerts() -> int:
    """Delete stale rows from alerts; optionally VACUUM ANALYZE."""
    from db import _put_conn, get_conn

    conn = get_conn()
    deleted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM alerts
                WHERE created_at < NOW() - make_interval(days => %s)
                """,
                (DATA_RETENTION_DAYS,),
            )
            deleted = cur.rowcount
            conn.commit()
            logger.info("Deleted %d alerts older than %d days", deleted, DATA_RETENTION_DAYS)
            if PURGE_VACUUM_ENABLED:
                cur.execute("VACUUM ANALYZE alerts")
                logger.info("VACUUM ANALYZE alerts completed")
    finally:
        _put_conn(conn)
    return deleted


def main() -> int:
    try:
        purge_old_alerts()
    except Exception as exc:
        logger.error("Purge failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
