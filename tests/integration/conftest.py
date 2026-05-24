"""Integration-test environment: fast Redis fail + autouse mock."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("MARKET_HOURS_GATES_ENABLED", "0")
os.environ.setdefault("REDIS_RETRY_ATTEMPTS", "1")
os.environ.setdefault("REDIS_RETRY_BACKOFF", "0.0")
os.environ.setdefault("REDIS_SOCKET_TIMEOUT", "0.5")


@pytest.fixture(autouse=True)
def _mock_redis_for_integration() -> MagicMock:
    """Mock Redis for validate_and_filter dedup/watch paths (no broker in local CI)."""
    mock_r = MagicMock()
    mock_r.set.return_value = True
    mock_r.get.return_value = None
    mock_r.delete.return_value = 1
    mock_r.incr.return_value = 1
    with patch("validate_and_filter.get_redis", return_value=mock_r):
        yield mock_r
