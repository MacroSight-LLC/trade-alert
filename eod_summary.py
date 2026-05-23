"""End-of-day pipeline summary posted to Discord ops channel at 4:15 PM ET.

Reads today's session stats from Redis and alert history from Postgres,
then sends a structured embed summarising the full trading day: runs,
candidates, alerts fired, gate rejection breakdown, and alert list.

Scheduled via crontab: 15 16 * * 1-5 (4:15 PM ET, TZ=America/New_York).
SSOT §13 — observability.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from log_config import configure_logging
from notifier_and_logger import send_ops_embed, send_ops_message

configure_logging()
logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def _session_int(session: dict[str, Any], key: str) -> int:
    raw = session.get(key, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _alert_created_et_date(created_at: Any) -> datetime.date | None:
    if created_at is None:
        return None
    if isinstance(created_at, str):
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    elif isinstance(created_at, datetime):
        created = created_at
    else:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.astimezone(_ET).date()


def _build_eod_embed() -> dict[str, Any]:
    from db import get_recent_alerts
    from redis_client import get_redis

    r = get_redis()
    today = datetime.now(tz=_ET).date().isoformat()
    today_et = datetime.now(tz=_ET).date()

    total_runs = 0
    total_candidates = 0
    total_fired = 0
    total_rejected = 0
    gate_totals: dict[str, int] = {}

    for tf in ["15m", "1h"]:
        session = r.hgetall(f"session:stats:{today}:{tf}") or {}
        total_runs += _session_int(session, "decision_runs")
        total_candidates += _session_int(session, "llm_candidates")
        total_fired += _session_int(session, "alerts_passed_total")
        total_rejected += _session_int(session, "alerts_rejected")
        for key, value in session.items():
            key_str = key.decode() if isinstance(key, bytes) else str(key)
            if key_str.startswith("gate_dir_"):
                gate = key_str.replace("gate_dir_", "")
                try:
                    gate_totals[gate] = gate_totals.get(gate, 0) + int(value)
                except (TypeError, ValueError):
                    pass

    top_gates = sorted(gate_totals.items(), key=lambda x: x[1], reverse=True)[:5]
    gate_str = "\n".join(f"`{g}`: {c}" for g, c in top_gates) if top_gates else "none"

    try:
        all_alerts = get_recent_alerts(limit=50)
        today_alerts = [a for a in all_alerts if _alert_created_et_date(a.get("created_at")) == today_et]
    except Exception as exc:
        logger.warning("EOD: could not fetch today's alerts — %s", exc)
        today_alerts = []

    alert_lines = []
    for a in today_alerts[:10]:
        sym = a.get("symbol", "?")
        direction = a.get("direction", "?")
        ep = float(a.get("edge_probability", 0))
        conf = float(a.get("confidence", 0))
        outcome = a.get("outcome") or "open"
        alert_lines.append(f"**{sym}** {direction} EP={ep:.0%} Conf={conf:.0%} [{outcome}]")
    alert_str = "\n".join(alert_lines) if alert_lines else "No alerts fired today"

    pass_rate = f"{total_fired / total_candidates:.0%}" if total_candidates > 0 else "N/A"
    rejection_pct = f"{total_rejected / max(total_candidates, 1):.0%}"

    color = 0x2ECC71 if total_fired > 0 else 0xE74C3C  # green / red

    embed = {
        "embeds": [
            {
                "title": f"📊 EOD Pipeline Summary — {today}",
                "color": color,
                "fields": [
                    {"name": "🔄 Decision Runs", "value": str(total_runs), "inline": True},
                    {"name": "🎯 Candidates Seen", "value": str(total_candidates), "inline": True},
                    {"name": "✅ Alerts Fired", "value": str(total_fired), "inline": True},
                    {"name": "❌ Rejected", "value": f"{total_rejected} ({rejection_pct})", "inline": True},
                    {"name": "📈 Pass Rate", "value": pass_rate, "inline": True},
                    {"name": "\u200b", "value": "\u200b", "inline": True},
                    {"name": "🚧 Top Gate Rejections", "value": gate_str, "inline": False},
                    {"name": "📣 Alerts", "value": alert_str, "inline": False},
                ],
                "timestamp": datetime.now(UTC).isoformat(),
                "footer": {"text": "trade-alert · EOD summary"},
            }
        ]
    }
    return embed


def main() -> None:
    today = datetime.now(tz=_ET).date().isoformat()
    logger.info("EOD summary: building embed for %s", today)
    try:
        embed = _build_eod_embed()
        send_ops_embed(embed)
        logger.info("EOD summary: sent successfully")
    except Exception as exc:
        logger.error("EOD summary failed: %s", exc)
        send_ops_message(f"❌ EOD summary failed: {exc}")


if __name__ == "__main__":
    main()
