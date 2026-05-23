"""Redis alert deduplication helpers."""

from __future__ import annotations

import os


def get_redis():
    """Resolve Redis client via validate_and_filter for test patch compatibility."""
    import validate_and_filter as vf

    return vf.get_redis()


def _circuit():
    from validate_and_filter import _check_redis_circuit, _record_redis_failure

    return _check_redis_circuit, _record_redis_failure


_DEDUP_TTL_SECONDS: int = int(os.environ.get("ALERT_DEDUP_TTL_SECONDS", "300"))
_DEDUP_ENABLED: bool = os.environ.get("ALERT_DEDUP_ENABLED", "1") == "1"
_WATCH_DEDUP_TTL_SECONDS: int = int(os.environ.get("WATCH_DEDUP_TTL_SECONDS", "900"))


def _dedup_key(symbol: str, direction: str, timeframe: str) -> str:
    return f"dedup:alert:{timeframe}:{direction}:{symbol}"


def _reset_dedup_keys(symbols: list[str], timeframe: str) -> None:
    """Clear WATCH and directional dedup keys when a symbol graduates."""
    if _circuit()[0]():
        return
    try:
        r = get_redis()
        for sym in symbols:
            for direction in ("LONG", "SHORT", "WATCH"):
                r.delete(_dedup_key(sym, direction, timeframe))
    except Exception:  # noqa: BLE001
        _circuit()[1]()


def _try_dedup_set(symbol: str, direction: str, timeframe: str) -> bool:
    """Return True if alert should be suppressed (dedup key already exists).

    Fail-open when dedup disabled or Redis circuit is open.
    """
    if not _DEDUP_ENABLED or _circuit()[0]():
        return False
    ttl = _WATCH_DEDUP_TTL_SECONDS if direction == "WATCH" else _DEDUP_TTL_SECONDS
    try:
        was_set = get_redis().set(_dedup_key(symbol, direction, timeframe), "1", nx=True, ex=ttl)
        return not was_set
    except Exception:  # noqa: BLE001
        _circuit()[1]()
        return False
