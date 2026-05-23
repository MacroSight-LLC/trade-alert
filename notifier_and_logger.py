"""Discord notifier and Postgres logger for trade-alert (compatibility shim).

Orchestrates validated PlaybookAlert delivery: dedup, chart generation,
Postgres persist-first, Discord send. Implements SSOT §11.

TODO: migrate callers to notifier.notify() directly, then remove this shim
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import time

import httpx
import redis
from pydantic import ValidationError

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from alert_logger import (
    batch_similar_alert_stats,
    extract_forecast_scores,
    get_similar_alert_stats,
    persist_alert,
)
from chart_gen import generate_chart
from constants import DEDUP_WINDOW_SECONDS, TRADE_EXECUTE_ENABLED
from db import is_execution_dispatched, mark_execution_dispatched
from discord_formatter import (
    _format_watch_embed,
    _quality_color,
    _route_channel_for_alert,
    _score_bar,
    _truncate_field,
    compute_rr,
    format_embed,
)
from execution_mapper import map_to_execution_payload
from execution_trigger import should_dispatch_execution
from execution_webhook import deliver_execution_payload
from log_config import configure_logging
from models import PlaybookAlert
from notifier import (
    send_discord_embed,
    send_ops_embed,
    send_ops_message,
)
from redis_client import get_redis

configure_logging()
logger = logging.getLogger(__name__)

MAX_ALERTS_PER_CYCLE: int = int(__import__("os").getenv("MAX_ALERTS_PER_CYCLE", "5"))

# Re-export for tests that patch historical stats lookup on this module.
_get_similar_alert_stats = get_similar_alert_stats
_batch_similar_alert_stats = batch_similar_alert_stats

__all__ = [
    "notify",
    "send_ops_message",
    "send_ops_embed",
    "send_discord_embed",
    "format_embed",
    "compute_rr",
    "_is_duplicate_alert",
    "_quality_color",
    "_score_bar",
    "_thesis_similarity",
    "_truncate_field",
]


def _thesis_similarity(a: str, b: str) -> float:
    """Jaccard similarity of thesis word sets."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _is_duplicate_alert(
    symbol: str,
    direction: str,
    timeframe: str,
    thesis: str = "",
) -> bool:
    """Check Redis for a recent alert with the same symbol/direction/timeframe."""
    thesis_hash = hashlib.md5(thesis[:120].encode()).hexdigest()[:8] if thesis else "no_thesis"
    dedup_key = f"alert:dedup:{symbol}:{direction}:{timeframe}:{thesis_hash}"
    thesis_key = f"alert:thesis:{symbol}:{direction}:{timeframe}"
    try:
        r = get_redis()
        was_set = r.set(dedup_key, "1", nx=True, ex=DEDUP_WINDOW_SECONDS)
        if was_set:
            if thesis:
                r.set(thesis_key, thesis, ex=DEDUP_WINDOW_SECONDS)
            return False

        if thesis:
            stored_thesis = r.get(thesis_key) or ""
            if stored_thesis and _thesis_similarity(thesis, stored_thesis) < 0.5:
                logger.info(
                    "Dedup: allowing new thesis for %s %s %s (different content)",
                    symbol,
                    direction,
                    timeframe,
                )
                pipe = r.pipeline()
                pipe.set(dedup_key, "1", ex=DEDUP_WINDOW_SECONDS)
                pipe.set(thesis_key, thesis, ex=DEDUP_WINDOW_SECONDS)
                pipe.execute()
                return False
        logger.info("Dedup: suppressing duplicate alert %s %s %s", symbol, direction, timeframe)
        return True
    except redis.RedisError as exc:
        logger.warning("Dedup check failed (allowing alert through): %s", exc)
        return False


def notify(
    alerts_json: str,
    raw_snapshots: list[dict] | None = None,
    trace_id: str | None = None,
) -> int:
    """Main entry point called by decision workflows."""
    snapshots = raw_snapshots or []
    forecast_scores = extract_forecast_scores(snapshots)
    n_sent = 0

    try:
        items = json.loads(alerts_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("Notifier JSON parse error: %s", exc)
        return 0

    if not isinstance(items, list):
        logger.error("Notifier expected list, got %s", type(items).__name__)
        return 0

    valid_alerts: list[PlaybookAlert] = []
    for item in items:
        try:
            if not isinstance(item, dict):
                logger.warning("Notifier: skipping non-dict item %s", type(item).__name__)
                continue
            alert = PlaybookAlert(**item)
            if _is_duplicate_alert(alert.symbol, alert.direction, alert.timeframe, alert.thesis):
                continue
            valid_alerts.append(alert)
        except (ValidationError, redis.RedisError, KeyError, TypeError) as exc:
            logger.error("Notifier alert processing failed: %s", exc)

    directional_alerts = [a for a in valid_alerts if a.direction in ("LONG", "SHORT")]
    watch_alerts = [a for a in valid_alerts if a.direction == "WATCH"]

    if len(directional_alerts) > MAX_ALERTS_PER_CYCLE:
        directional_alerts.sort(
            key=lambda a: a.edge_probability * a.confidence,
            reverse=True,
        )
        dropped = len(directional_alerts) - MAX_ALERTS_PER_CYCLE
        directional_alerts = directional_alerts[:MAX_ALERTS_PER_CYCLE]
        logger.warning(
            "Capped directional alerts: dropped %d of %d (kept top %d by EP*conf)",
            dropped,
            dropped + MAX_ALERTS_PER_CYCLE,
            MAX_ALERTS_PER_CYCLE,
        )

    if len(watch_alerts) > 1:
        watch_alerts.sort(
            key=lambda a: a.edge_probability * a.confidence,
            reverse=True,
        )
        dropped_watch = len(watch_alerts) - 1
        watch_alerts = watch_alerts[:1]
        logger.info("Capped WATCH alerts: dropped %d (kept top 1)", dropped_watch)

    valid_alerts = directional_alerts + watch_alerts

    batch_stats = batch_similar_alert_stats(valid_alerts)
    chart_map: dict[str, bytes | None] = {}
    atr_map: dict[str, float | None] = {}
    current_price_map: dict[str, float | None] = {}
    current_price_ts_map: dict[str, str | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        future_to_sym = {
            pool.submit(generate_chart, alert.symbol, alert.timeframe, alert.entry): alert.symbol
            for alert in valid_alerts
            if alert.direction != "WATCH"
        }
        for future in concurrent.futures.as_completed(future_to_sym):
            sym = future_to_sym[future]
            try:
                chart_bytes, atr_val, current_price, current_price_ts = future.result()
                chart_map[sym] = chart_bytes
                atr_map[sym] = atr_val
                current_price_map[sym] = current_price
                current_price_ts_map[sym] = current_price_ts
            except Exception as exc:
                logger.warning("Chart generation failed for %s: %s", sym, exc)
                chart_map[sym] = None
                atr_map[sym] = None
                current_price_map[sym] = None
                current_price_ts_map[sym] = None

    for alert in valid_alerts:
        try:
            if alert.direction == "WATCH":
                embed = _format_watch_embed(alert)
            else:
                embed = format_embed(
                    alert,
                    hist_stats=batch_stats.get(f"{alert.symbol}:{alert.direction}", ""),
                    current_price=current_price_map.get(alert.symbol),
                    current_price_ts=current_price_ts_map.get(alert.symbol),
                )
            chart_png = chart_map.get(alert.symbol)
            atr_val = atr_map.get(alert.symbol)
            if chart_png and alert.direction != "WATCH":
                embed["embeds"][0]["image"] = {"url": "attachment://chart.png"}
            if atr_val and atr_val > 0 and alert.direction != "WATCH":
                embed["embeds"][0]["fields"].append(
                    {
                        "name": "\U0001f4b0 ATR Risk Guide",
                        "value": (
                            f"14-period ATR: **${atr_val:,.2f}**\n"
                            f"1 ATR stop: ${atr_val * 1.5:,.2f} risk/share"
                        ),
                        "inline": True,
                    }
                )

            persisted = persist_alert(alert, snapshots, forecast_scores, trace_id=trace_id)
            if persisted is None:
                continue
            alert_id, idempotency_key = persisted

            if TRADE_EXECUTE_ENABLED and alert.direction in ("LONG", "SHORT"):
                try:
                    payload = map_to_execution_payload(
                        alert,
                        alert_id=str(alert_id),
                        idempotency_key=idempotency_key,
                        pipeline_trace_id=trace_id or None,
                    )
                    if should_dispatch_execution(
                        expires_at=payload.expires_at,
                        idempotency_key=payload.idempotency_key,
                        already_dispatched=is_execution_dispatched(alert_id),
                    ) and deliver_execution_payload(payload):
                        mark_execution_dispatched(alert_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Execution webhook failed for %s — continuing to Discord: %s",
                        alert.symbol,
                        exc,
                    )

            routed_channel = _route_channel_for_alert(alert)
            sent = send_discord_embed(embed, chart_png=chart_png, channel_override=routed_channel)
            if sent:
                n_sent += 1
            else:
                logger.warning(
                    "Discord send failed for %s after successful Postgres insert",
                    alert.symbol,
                )
        except (httpx.HTTPError, redis.RedisError, KeyError, TypeError, ValueError) as exc:
            logger.error("Notifier alert send failed: %s", exc)

    logger.info("Notifier: sent %d/%d alerts to Discord", n_sent, len(items))
    return n_sent


if __name__ == "__main__":
    sample_alert = PlaybookAlert(
        symbol="NVDA",
        direction="LONG",
        edge_probability=0.82,
        confidence=0.85,
        timeframe="15m",
        thesis="Bollinger Band squeeze breaking out with 3x volume and "
        "strong retail sentiment. Institutional order flow confirms.",
        entry={"level": 875.0, "stop": 865.0, "target": 900.0},
        timeframe_rationale="15m breakout aligning with 1h uptrend structure.",
        sentiment_context="ROT strong_bullish, Finnhub +0.6 aggregate score.",
        unusual_activity=["IV spike 2.1x avg", "options sweep $900c 0DTE"],
        macro_regime="Risk-on. VIX 13.2, yield curve +18bps.",
        sources_agree=5,
    )

    embed = format_embed(sample_alert)
    rr = compute_rr(sample_alert.entry)

    print("=== DISCORD EMBED (dry-run) ===")
    print(json.dumps(embed, indent=2))
    print(f"\nR:R computed: {rr}")
    print(f"Title: {embed['embeds'][0]['title']}")
    print("\nNotifier dry-run \u2705")
