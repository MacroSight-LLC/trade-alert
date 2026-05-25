"""WATCH policy lifecycle helpers."""

from __future__ import annotations

import os


def get_redis():
    """Resolve Redis via validate_and_filter for test patch compatibility."""
    from validate_and_filter import get_redis as _vf_get_redis

    return _vf_get_redis()


def _circuit():
    from gates.redis_circuit import _check_redis_circuit, _record_redis_failure

    return _check_redis_circuit, _record_redis_failure


_WATCH_MAX_PER_RUN: int = int(os.environ.get("WATCH_MAX_PER_RUN", "1"))
_WATCH_MAX_STRESSED: int = int(os.environ.get("WATCH_MAX_STRESSED", str(_WATCH_MAX_PER_RUN)))
_WATCH_MAX_NEUTRAL: int = int(os.environ.get("WATCH_MAX_NEUTRAL", "2"))
_WATCH_MAX_TRENDING: int = int(os.environ.get("WATCH_MAX_TRENDING", "3"))
_WATCH_DECAY_TTL_SECONDS: int = int(os.environ.get("WATCH_DECAY_TTL_SECONDS", str(60 * 60 * 24)))


def _watch_max_for_regime(regime: str) -> int:
    """Return the maximum number of WATCH alerts to emit for a given regime."""
    from gate_config import WATCH_MAX_NEUTRAL, WATCH_MAX_STRESSED, WATCH_MAX_TRENDING

    if regime in ("extreme", "risk_off_high_vix"):
        return WATCH_MAX_STRESSED
    if regime in ("choppy", "neutral"):
        return WATCH_MAX_NEUTRAL
    return WATCH_MAX_TRENDING


def _watch_decay_key(symbol: str, timeframe: str) -> str:
    return f"watch:decay:{timeframe}:{symbol}"


def _get_watch_cycles(symbol: str, timeframe: str) -> int:
    """Return the number of consecutive pipeline cycles a WATCH has persisted."""
    if _circuit()[0]():
        return 0
    try:
        val = get_redis().hget(_watch_decay_key(symbol, timeframe), "cycles")
        return int(val) if val else 0
    except Exception:  # noqa: BLE001
        _circuit()[1]()
        return 0


def _incr_watch_cycles(symbol: str, timeframe: str, ep: float, conf: float) -> int:
    """Increment the watch cycle counter. Returns the new cycle count."""
    if _circuit()[0]():
        return 0
    try:
        r = get_redis()
        key = _watch_decay_key(symbol, timeframe)
        pipe = r.pipeline()
        pipe.hincrby(key, "cycles", 1)
        pipe.hset(key, mapping={"last_ep": str(ep), "last_conf": str(conf)})
        pipe.expire(key, _WATCH_DECAY_TTL_SECONDS)
        results = pipe.execute()
        return int(results[0])
    except Exception:  # noqa: BLE001
        _circuit()[1]()
        return 0


def _reset_watch_cycles(symbols: list[str], timeframe: str) -> None:
    """Delete watch-cycle state for symbols that graduated to a directional alert."""
    if _circuit()[0]():
        return
    try:
        r = get_redis()
        for sym in symbols:
            r.delete(_watch_decay_key(sym, timeframe))
    except Exception:  # noqa: BLE001
        _circuit()[1]()


def _get_watch_prev_state(symbol: str, timeframe: str) -> dict[str, str] | None:
    """Return the Redis watch-cycle state dict for symbol, or None if absent."""
    if _circuit()[0]():
        return None
    try:
        r = get_redis()
        state = r.hgetall(_watch_decay_key(symbol, timeframe))
        if state:
            return {
                k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in state.items()
            }
        return None
    except Exception:  # noqa: BLE001
        _circuit()[1]()
        return None


def _watch_is_improving(
    symbol: str,
    current_ep: float,
    prev_states: dict[str, dict[str, str] | None],
) -> bool:
    """Return True if the symbol's current EP exceeds its recorded prev EP."""
    state = prev_states.get(symbol)
    if not state:
        return False
    try:
        return current_ep > float(state.get("last_ep", 0.0))
    except (TypeError, ValueError):
        return False
