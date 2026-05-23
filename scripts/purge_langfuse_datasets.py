#!/usr/bin/env python3
"""Prune Langfuse dataset items older than LANGFUSE_DATASET_RETENTION_DAYS."""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

RETENTION_DAYS = int(os.environ.get("LANGFUSE_DATASET_RETENTION_DAYS", "90"))
DATASETS = ("decision-runs", "decision-golden")


def purge_datasets() -> int:
    from langfuse_client import get_langfuse_client

    lf = get_langfuse_client()
    if lf is None:
        logger.warning("Langfuse client unavailable — skipping purge")
        return 0

    cutoff = datetime.now(tz=UTC) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for name in DATASETS:
        try:
            dataset = lf.get_dataset(name)
            for item in dataset.items or []:
                created = getattr(item, "created_at", None)
                if created and created < cutoff:
                    lf.api.dataset_items.delete(id=item.id)
                    removed += 1
        except Exception as exc:
            logger.warning("Dataset %s purge error: %s", name, exc)
    logger.info("Purged %d Langfuse dataset items older than %d days", removed, RETENTION_DAYS)
    return removed


def main() -> int:
    try:
        purge_datasets()
    except Exception as exc:
        logger.error("Langfuse purge failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
