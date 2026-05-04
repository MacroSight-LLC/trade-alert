"""Events normalizer — earnings calendar and material filings (SSOT §7).

Transforms earnings proximity and 8-K filings into ``catalyst_event`` signals.

Note:
    EDGAR Form 4 insider *clustering* (≥3 distinct insiders, dollar-value
    scoring) is handled by ``market_normalizer._add_edgar_insider_signals``
    which produces ``insider_activity`` signals.  This normalizer focuses on
    8-K material events and earnings calendar → ``catalyst_event`` signals.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot

_log = logging.getLogger(__name__)


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert earnings calendar and filing data into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value may contain:
            - earnings_date (str): ISO date of next earnings
            - eps_estimate (float|None): Consensus EPS estimate
            - revenue_estimate (float|None): Revenue estimate
            - hour (str): "bmo" | "amc" | "" (before/after market)
            - recent_8k (bool): Whether an 8-K was filed in last 24h
            - filing_count (int): Number of material filings in last 7d
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots for symbols with catalyst events.
    """
    snapshots: list[Snapshot] = []
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []

        # Earnings proximity scoring
        earnings_date_str: str | None = data.get("earnings_date")
        if earnings_date_str:
            try:
                earnings_dt = datetime.strptime(earnings_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                hours_until = (earnings_dt - now_dt).total_seconds() / 3600.0

                if -24 <= hours_until <= 168:  # 168h = 7 days; include recent (last 24h)
                    # Continuous scoring: closer earnings = higher score.
                    # Map [0..168] hours → score [2.5..0.5], conf [0.90..0.50]
                    # Past earnings (< 0h): decay rapidly — alpha already priced in.
                    # -24h → score 0.5, conf 0.40  (post-report residual volatility)
                    if hours_until < 0:
                        # Decay: 0h → (1.5, 0.60), -24h → (0.5, 0.40)
                        t_past = min(abs(hours_until) / 24.0, 1.0)
                        raw_score = 1.5 - t_past * 1.0  # 1.5 → 0.5
                        conf = 0.60 - t_past * 0.20  # 0.60 → 0.40
                    else:
                        t = (earnings_dt - now_dt).days / 7.0  # 0.0 = imminent, 1.0 = 7d out
                        raw_score = 2.5 - t * 2.0  # 2.5 → 0.5
                        conf = 0.90 - t * 0.40  # 0.90 → 0.50

                    days_until = max(int(hours_until / 24), 0)
                    if hours_until < 0:
                        reason = f"Earnings RECENT ({earnings_date_str}, {abs(hours_until):.0f}h ago)"
                    elif hours_until <= 24:
                        reason = f"Earnings IMMINENT ({earnings_date_str})"
                    elif hours_until <= 48:
                        reason = f"Earnings TOMORROW ({earnings_date_str})"
                    else:
                        reason = f"Earnings in {days_until}d ({earnings_date_str})"

                    hour = data.get("hour", "")
                    if hour:
                        reason += f" [{hour.upper()}]"

                    eps = data.get("eps_estimate")
                    if eps is not None:
                        reason += f", EPS est ${eps:.2f}"

                    signals.append(
                        Signal(
                            source="finnhub",
                            type="catalyst_event",
                            score=raw_score,
                            confidence=conf,
                            reason=reason,
                            raw=data,
                        )
                    )
            except (ValueError, TypeError):
                _log.warning("Unparseable earnings date for %s: %r", symbol, earnings_date_str)

        # 8-K / material filing scoring
        recent_8k: bool = data.get("recent_8k", False)
        filing_count: int = data.get("filing_count", 0)

        if recent_8k:
            signals.append(
                Signal(
                    source="edgar",
                    type="catalyst_event",
                    score=2.0,
                    confidence=0.75,
                    reason="8-K material event filed in last 24h",
                    raw=data,
                )
            )
        elif filing_count >= 2:
            signals.append(
                Signal(
                    source="edgar",
                    type="catalyst_event",
                    score=1.0,
                    confidence=0.60,
                    reason=f"{filing_count} material filings in last 7d",
                    raw=data,
                )
            )

        if signals:
            snapshots.append(
                Snapshot(
                    symbol=symbol,
                    timeframe=cast(Literal["5m", "15m", "1h", "4h", "1D"], timeframe),
                    timestamp=now,
                    signals=signals,
                )
            )

    return snapshots
