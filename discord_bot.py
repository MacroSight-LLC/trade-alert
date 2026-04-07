"""Lightweight Discord bot for manual pipeline triggers.

Listens in the ops channel for commands:
    !scan         — Run the 15m pipeline now
    !scan 1h      — Run the 1h pipeline now
    !scan 15m     — Run the 15m pipeline now (explicit)
    !status       — Show pipeline health summary
    !last         — Show most recent fired alert
    !help         — Show available commands

The bot runs inside the cuga container (same image) so it has full
access to pipeline_runner, Redis, Postgres, and all MCP endpoints.

SSOT §11 addendum — manual trigger interface.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import vault_env_loader  # noqa: F401 — loads Vault secrets into os.environ
from constants import get_market_hours_status, is_early_close, is_holiday
from log_config import configure_logging
from redis_client import get_redis

configure_logging()
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.getenv("DISCORD_BOT_TOKEN", "")
OPS_CHANNEL_ID: str = os.getenv("DISCORD_OPS_CHANNEL_ID", "")
API_BASE = "https://discord.com/api/v10"
POLL_INTERVAL: float = float(os.getenv("DISCORD_BOT_POLL_INTERVAL", "3.0"))

# Track processed message IDs to avoid re-processing
_processed: set[str] = set()
_MAX_PROCESSED: int = 500

# Concurrency lock — prevents overlapping !scan invocations
_scan_lock = threading.Lock()

# Graceful shutdown event
_shutdown = threading.Event()
_ET = ZoneInfo("America/New_York")

# Shared HTTP client (reused across all calls)
_http_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Return shared httpx client, creating if needed."""
    global _http_client  # noqa: PLW0603
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(timeout=10.0)
    return _http_client


def _headers() -> dict[str, str]:
    """Authorization headers for Discord bot API calls."""
    return {"Authorization": f"Bot {BOT_TOKEN}"}


def _send_message(channel_id: str, content: str) -> None:
    """Send a plain text message to a Discord channel.

    Args:
        channel_id: Target channel.
        content: Message body (max 2000 chars).
    """
    try:
        resp = _get_client().post(
            f"{API_BASE}/channels/{channel_id}/messages",
            json={"content": content[:2000]},
            headers=_headers(),
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Failed to send message: %s", exc)


def _send_embed(channel_id: str, embed: dict) -> None:
    """Send a rich embed to a Discord channel.

    Args:
        channel_id: Target channel.
        embed: Discord embed dict.
    """
    try:
        resp = _get_client().post(
            f"{API_BASE}/channels/{channel_id}/messages",
            json={"embeds": [embed]},
            headers=_headers(),
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("Failed to send embed: %s", exc)


def _run_pipeline(timeframe: str) -> tuple[bool, str]:
    """Execute the orchestrator workflow synchronously.

    Args:
        timeframe: ``"15m"`` or ``"1h"``.

    Returns:
        Tuple of (success, summary_message).
    """
    # Whitelist to prevent path traversal
    allowed_timeframes = {"15m", "1h"}
    if timeframe not in allowed_timeframes:
        return False, f"Invalid timeframe: {timeframe}. Allowed: {', '.join(sorted(allowed_timeframes))}"

    workflow = f"workflows/orchestrator-{timeframe}.yaml"
    if not Path(workflow).exists():
        return False, f"Workflow not found: {workflow}"

    logger.info("Triggering pipeline: %s", workflow)
    start = time.monotonic()

    try:
        result = subprocess.run(
            [sys.executable, "pipeline_runner.py", workflow],
            capture_output=True,
            text=True,
            timeout=300,
            cwd="/app",
        )
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            # Parse output for alert count
            lines = result.stdout.strip().split("\n")
            alert_info = ""
            for line in lines:
                if "sent" in line.lower() and "alert" in line.lower():
                    alert_info = line.strip()
                elif "no alerts" in line.lower():
                    alert_info = line.strip()
            summary = f"Pipeline {timeframe} completed in {elapsed:.1f}s"
            if alert_info:
                summary += f"\n{alert_info}"
            return True, summary
        else:
            stderr_tail = (result.stderr or "")[-500:]
            return False, f"Pipeline {timeframe} failed (exit {result.returncode})\n```{stderr_tail}```"
    except subprocess.TimeoutExpired:
        return False, f"Pipeline {timeframe} timed out after 300s"
    except FileNotFoundError as exc:
        return False, f"Pipeline runner not found: {exc}"


def _get_status() -> str:
    """Build a pipeline health status summary.

    Returns:
        Formatted status string.
    """
    from redis_client import get_redis as _get_status_redis

    lines: list[str] = ["**Pipeline Status**\n"]

    # Redis snapshot counts
    r = None
    try:
        r = _get_status_redis()
        for tf in ["15m", "1h"]:
            count = r.llen(f"snapshots:{tf}")
            lines.append(f"  `snapshots:{tf}`: **{count}** entries")

        # Count signal types in 15m
        count_15m = r.llen("snapshots:15m")
        if count_15m > 0:
            type_counts: Counter[str] = Counter()
            for i in range(min(count_15m, 200)):
                raw = r.lindex("snapshots:15m", i)
                if raw:
                    snap = json.loads(raw)
                    for sig in snap.get("signals", []):
                        type_counts[sig.get("type", "unknown")] += 1
            if type_counts:
                lines.append("  **Signal types (15m):**")
                for t, c in type_counts.most_common():
                    lines.append(f"    {t}: {c}")
    except Exception as exc:
        lines.append(f"  Redis: error — {exc}")

    # MCP health
    lines.append("\n**MCP Servers:**")
    mcp_ports = {
        8001: "TradingView",
        8002: "Polygon",
        8003: "Discord",
        8004: "Finnhub",
        8005: "ROT",
        8006: "EDGAR",
        8007: "YFinance",
        8008: "Trading",
        8009: "FRED",
        8010: "SpamShield",
        8011: "Alpaca",
    }
    for port, name in mcp_ports.items():
        host = name.lower().replace(" ", "-") + "-mcp"
        try:
            resp = _get_client().get(f"http://{host}:{port}/health", timeout=3.0)
            if resp.status_code == 200:
                lines.append(f"  {name} (:{port}): healthy")
            else:
                lines.append(f"  {name} (:{port}): unhealthy ({resp.status_code})")
        except httpx.HTTPError:
            lines.append(f"  {name} (:{port}): unreachable")

    return "\n".join(lines)


def _handle_command(content: str, channel_id: str) -> None:
    """Process a bot command and send response to Discord.

    Args:
        content: Raw message text.
        channel_id: Channel where command was received.
    """
    parts = content.strip().lower().split()
    cmd = parts[0] if parts else ""

    if cmd == "!scan":
        timeframe = "15m"
        if len(parts) > 1 and parts[1] in ("1h", "15m"):
            timeframe = parts[1]

        if not _scan_lock.acquire(blocking=False):
            _send_message(channel_id, "⚠️ A scan is already running. Please wait.")
            return
        try:
            _send_message(channel_id, f"🔍 Triggering **{timeframe}** pipeline scan...")
            success, summary = _run_pipeline(timeframe)
            emoji = "✅" if success else "❌"
            _send_message(channel_id, f"{emoji} {summary}")
        finally:
            _scan_lock.release()

    elif cmd == "!status":
        _send_message(channel_id, "⚙️ Gathering status...")
        status = _get_status()
        _send_message(channel_id, status)

    elif cmd == "!last":
        _send_last_alert(channel_id)

    elif cmd == "!session":
        _send_message(channel_id, "📊 Building session report...")
        _send_message(channel_id, _build_session_report())

    elif cmd == "!help":
        _send_message(
            channel_id,
            "**Trade Alert Bot Commands:**\n"
            "`!scan` — Run the 15m pipeline now\n"
            "`!scan 15m` — Run the 15m pipeline (explicit)\n"
            "`!scan 1h` — Run the 1h pipeline\n"
            "`!status` — Show pipeline health & Redis snapshot counts\n"
            "`!session` — Show alert-rate and gate-rejection summary for the current/last session\n"
            "`!last` — Show most recent fired alert\n"
            "`!help` — Show this message",
        )


def _previous_trading_day(start: date) -> date:
    cursor = start - timedelta(days=1)
    while cursor.weekday() >= 5 or is_holiday(cursor):
        cursor -= timedelta(days=1)
    return cursor


def _session_window(now: datetime | None = None) -> tuple[str, datetime, datetime, date]:
    now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)

    if now_et.weekday() >= 5 or is_holiday(now_et.date()):
        session_date = _previous_trading_day(now_et.date())
        label = "Last Completed Session"
    else:
        close_time = dt_time(13, 0) if is_early_close(now_et.date()) else dt_time(16, 0)
        session_open = datetime.combine(now_et.date(), dt_time(9, 30), tzinfo=_ET)
        session_close = datetime.combine(now_et.date(), close_time, tzinfo=_ET)
        if now_et < session_open:
            session_date = _previous_trading_day(now_et.date())
            label = "Last Completed Session"
        else:
            session_date = now_et.date()
            label = "Current Session So Far" if now_et < session_close else "Last Completed Session"

    close_time = dt_time(13, 0) if is_early_close(session_date) else dt_time(16, 0)
    start_et = datetime.combine(session_date, dt_time(9, 30), tzinfo=_ET)
    end_et = datetime.combine(session_date, close_time, tzinfo=_ET)
    return label, start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc), session_date


def _format_gate_counts(stats: dict[str, str]) -> str:
    gate_counts: list[tuple[str, int]] = []
    for key, value in stats.items():
        if not key.startswith("gate_"):
            continue
        try:
            gate_counts.append((key.removeprefix("gate_"), int(value)))
        except ValueError:
            continue
    if not gate_counts:
        return "none"
    gate_counts.sort(key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{name}={count}" for name, count in gate_counts[:5])


def _session_stats(timeframe: str, session_date: date) -> dict[str, str]:
    try:
        redis_client = get_redis()
        return redis_client.hgetall(f"session:stats:{session_date.isoformat()}:{timeframe}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read session stats from Redis: %s", exc)
        return {}


def _build_session_report() -> str:
    try:
        import psycopg2

        label, start_utc, end_utc, session_date = _session_window()
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            return "❌ DATABASE_URL not set — cannot build session report"

        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN direction = 'LONG' THEN 1 ELSE 0 END) AS longs,
                        SUM(CASE WHEN direction = 'SHORT' THEN 1 ELSE 0 END) AS shorts,
                        SUM(CASE WHEN direction = 'WATCH' THEN 1 ELSE 0 END) AS watches
                    FROM alerts
                    WHERE created_at >= %s AND created_at < %s
                    """,
                    (start_utc, end_utc),
                )
                totals = cur.fetchone() or (0, 0, 0, 0)

                cur.execute(
                    """
                    SELECT timeframe, COUNT(*)
                    FROM alerts
                    WHERE created_at >= %s AND created_at < %s
                    GROUP BY timeframe
                    ORDER BY timeframe
                    """,
                    (start_utc, end_utc),
                )
                timeframe_rows = cur.fetchall()
        finally:
            conn.close()

        timeframe_counts = {tf: count for tf, count in timeframe_rows}
        lines = [
            f"**{label}**",
            f"Session: {session_date.isoformat()} ET",
            f"Market: {get_market_hours_status()}",
            f"Alerts: **{totals[0]}** total | LONG {totals[1] or 0} | SHORT {totals[2] or 0} | WATCH {totals[3] or 0}",
        ]
        if timeframe_counts:
            lines.append(
                "By timeframe: " + ", ".join(f"{tf}={count}" for tf, count in sorted(timeframe_counts.items()))
            )

        total_runs = 0
        for timeframe in ("15m", "1h"):
            stats = _session_stats(timeframe, session_date)
            if not stats:
                lines.append(f"{timeframe}: no decision telemetry yet")
                continue

            runs = int(stats.get("decision_runs", "0") or 0)
            total_runs += runs
            llm_candidates = int(stats.get("llm_candidates", "0") or 0)
            alerts_passed = int(stats.get("alerts_passed", "0") or 0)
            alerts_rejected = int(stats.get("alerts_rejected", "0") or 0)
            watch_kept = int(stats.get("watch_kept", "0") or 0)
            watch_dropped_directional_present = int(
                stats.get("gate_watch_dropped_directional_present", "0") or 0
            )
            watch_cap_rejections = int(stats.get("gate_watch_cap", "0") or 0)
            alert_rate = (alerts_passed / runs) if runs else 0.0
            pass_rate = (alerts_passed / llm_candidates) if llm_candidates else 0.0
            lines.append(
                f"{timeframe}: runs={runs} | llm_candidates={llm_candidates} | passed={alerts_passed} | rejected={alerts_rejected} | alerts/run={alert_rate:.2f} | pass_rate={pass_rate:.0%}"
            )
            lines.append(
                f"{timeframe} watch: kept={watch_kept} | dropped_directional_present={watch_dropped_directional_present} | cap_rejections={watch_cap_rejections}"
            )
            lines.append(f"{timeframe} top rejections: {_format_gate_counts(stats)}")

        if totals[0] and total_runs == 0:
            lines.append("Note: alerts exist for this session, but gate telemetry started after the latest deploy/restart.")

        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to build session report: %s", exc)
        return f"❌ Could not build session report: {exc}"


def _send_last_alert(channel_id: str) -> None:
    """Fetch and display the most recent alert from Postgres.

    Args:
        channel_id: Channel to send the response to.
    """
    try:
        import psycopg2

        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            logger.error("DATABASE_URL not set")
            return
        conn = psycopg2.connect(db_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol, direction, edge_probability, sources_agree, "
                    "confidence, thesis, timeframe, created_at, outcome "
                    "FROM alerts ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            _send_message(channel_id, "No alerts in the database yet.")
            return

        sym, direction, ep, sa, conf, thesis, tf, created, outcome = row
        outcome_str = f" → **{outcome}**" if outcome else ""
        _send_message(
            channel_id,
            f"**Last Alert{outcome_str}**\n"
            f"**{sym}** {direction} ({tf})\n"
            f"EP: {ep:.0%} | SA: {sa}/10 | Conf: {conf:.0%}\n"
            f"_{thesis[:200]}_\n"
            f"Fired: {created:%Y-%m-%d %H:%M UTC}",
        )
    except Exception as exc:
        logger.error("Failed to fetch last alert: %s", exc)
        _send_message(channel_id, f"❌ Could not fetch last alert: {exc}")


def _poll_messages() -> None:
    """Poll the ops channel for new commands.

    Uses ``?after=`` to efficiently fetch only new messages.
    Tracks the last-seen message ID to avoid re-processing.
    """
    global _processed  # noqa: PLW0603

    last_id = "0"

    logger.info(
        "Bot started — polling channel %s every %.1fs",
        OPS_CHANNEL_ID,
        POLL_INTERVAL,
    )
    _send_message(
        OPS_CHANNEL_ID,
        "Bot online. Type `!help` for available commands.",
    )

    while not _shutdown.is_set():
        try:
            params: dict[str, str | int] = {"limit": 10}
            if last_id != "0":
                params["after"] = last_id
            resp = _get_client().get(
                f"{API_BASE}/channels/{OPS_CHANNEL_ID}/messages",
                headers=_headers(),
                params=params,
            )
            resp.raise_for_status()
            messages = resp.json()

            # Messages come newest-first, reverse for chronological order
            for msg in reversed(messages):
                msg_id = msg["id"]
                if msg_id in _processed:
                    continue

                # Update last_id to the newest message we've seen
                if int(msg_id) > int(last_id):
                    last_id = msg_id

                _processed.add(msg_id)

                # Trim processed set to avoid unbounded growth
                if len(_processed) > _MAX_PROCESSED:
                    _processed = set(sorted(_processed, key=int)[-_MAX_PROCESSED // 2 :])

                # Skip bot's own messages
                author = msg.get("author", {})
                if author.get("bot"):
                    continue

                content = msg.get("content", "").strip()
                if content.startswith("!"):
                    logger.info("Command from %s: %s", author.get("username", "?"), content)
                    _handle_command(content, OPS_CHANNEL_ID)

        except httpx.HTTPError as exc:
            logger.warning("Poll error: %s", exc)
        except Exception as exc:
            logger.error("Unexpected poll error: %s", exc)

        time.sleep(POLL_INTERVAL)

    # Clean up on shutdown
    logger.info("Shutting down Discord bot gracefully")
    client = _get_client()
    if client and not client.is_closed:
        client.close()


def _shutdown_handler(signum: int, _frame: object) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    logger.info("Received signal %d — initiating shutdown", signum)
    _shutdown.set()


def main() -> None:
    """Entry point for the Discord bot."""
    if not BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN not set — cannot start bot")
        sys.exit(1)
    if not OPS_CHANNEL_ID:
        logger.error("DISCORD_OPS_CHANNEL_ID not set — cannot start bot")
        sys.exit(1)

    logger.info("Starting Discord bot (polling mode)")
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    _poll_messages()


if __name__ == "__main__":
    main()
