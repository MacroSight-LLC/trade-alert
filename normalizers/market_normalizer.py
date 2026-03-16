"""Market universe normalizer (SSOT §8).

Transforms price-change and insider-activity signals into scored Snapshots.
Used by collector-market to produce supplementary universe-quality signals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, cast

from models import Signal, Snapshot
from normalizers import normalize_score


def normalize(raw_results: dict[str, Any], *, timeframe: str) -> list[Snapshot]:
    """Convert market screening data into Snapshots.

    Args:
        raw_results: Dict keyed by symbol. Each value may contain:
            - price_change_24h (float): 24h price change percentage
            - insider_activity (str): "buying", "selling", or "none"
        timeframe: Candle timeframe, e.g. "15m".

    Returns:
        List of Snapshots for symbols with actionable signals.
    """
    snapshots: list[Snapshot] = []
    now = datetime.now(timezone.utc).isoformat()

    for symbol, data in raw_results.items():
        signals: list[Signal] = []

        change: float | None = data.get("price_change_24h")
        if change is not None:
            abs_change = abs(change)
            if abs_change >= 5.0:
                score = 2.5 if change > 0 else -2.5
                conf = 0.8
            elif abs_change >= 2.0:
                score = 1.5 if change > 0 else -1.5
                conf = 0.65
            else:
                score = None
                conf = None
            if score is not None:
                signals.append(
                    Signal(
                        source="trading",
                        type="technical_trend",
                        score=normalize_score(score, -3.0, 3.0),
                        confidence=conf,
                        reason=f"24h change {change:+.1f}%",
                        raw=data,
                    )
                )

        # Relative strength vs SPY (SSOT §7)
        pct_change: float | None = data.get("price_change_24h")
        spy_change: float | None = data.get("spy_pct_change")
        if pct_change is not None and spy_change is not None:
            rs = pct_change - spy_change
            abs_rs = abs(rs)
            if abs_rs >= 2.0:
                rs_score = min(abs_rs / 2.0, 3.0) if rs > 0 else max(-abs_rs / 2.0, -3.0)
                signals.append(
                    Signal(
                        source="polygon",
                        type="relative_strength",
                        score=normalize_score(rs_score, -3.0, 3.0),
                        confidence=min(abs_rs / 10.0, 1.0),
                        reason=f"RS vs SPY {rs:+.1f}% (sym {pct_change:+.1f}%, SPY {spy_change:+.1f}%)",
                        raw=data,
                    )
                )

        # Enhanced insider activity: prefer EDGAR filing data when available,
        # fall back to Finnhub binary buying/selling classification.
        edgar_filings: list[dict] = data.get("edgar_filings", [])
        if edgar_filings:
            _add_edgar_insider_signals(signals, edgar_filings, data)
        else:
            insider: str | None = data.get("insider_activity")
            if insider:
                insider_lower = insider.strip().lower()
                if insider_lower in ("buying", "purchase", "buy"):
                    signals.append(
                        Signal(
                            source="trading",
                            type="insider_activity",
                            score=normalize_score(1.5, -3.0, 3.0),
                            confidence=0.75,
                            reason="Insider buying activity",
                            raw=data,
                        )
                    )
                elif insider_lower in ("selling", "sale", "disposition", "sell"):
                    signals.append(
                        Signal(
                            source="trading",
                            type="insider_activity",
                            score=normalize_score(-1.5, -3.0, 3.0),
                            confidence=0.75,
                            reason="Insider selling activity",
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


def _add_edgar_insider_signals(
    signals: list[Signal],
    filings: list[dict],
    raw_data: dict,
) -> None:
    """Score EDGAR Form 4 filings by dollar value and cluster density."""
    buy_value = 0.0
    sell_value = 0.0
    buy_count = 0
    insiders_buying: set[str] = set()

    for f in filings:
        txn = (f.get("transaction_type") or "").lower()
        value = abs(float(f.get("value") or 0))
        insider_name = f.get("insider", "unknown")

        if txn in ("purchase", "buy", "p"):
            buy_value += value
            buy_count += 1
            insiders_buying.add(insider_name)
        elif txn in ("sale", "sell", "s", "disposition"):
            sell_value += value
        else:
            # "filing" type from EDGAR search (buy/sell unknown) — count insiders
            insiders_buying.add(insider_name)

    # Score by dollar value when available, otherwise by filing count
    total_filings = len(filings)
    has_dollar_data = buy_value > 0 or sell_value > 0

    if has_dollar_data:
        if buy_value >= 1_000_000:
            raw_score, conf = 2.5, 0.90
            reason = f"EDGAR insider purchases ${buy_value:,.0f}"
        elif buy_value >= 500_000:
            raw_score, conf = 2.0, 0.80
            reason = f"EDGAR insider purchases ${buy_value:,.0f}"
        elif buy_value >= 100_000:
            raw_score, conf = 1.0, 0.65
            reason = f"EDGAR insider purchases ${buy_value:,.0f}"
        elif sell_value >= 1_000_000:
            raw_score, conf = -2.0, 0.80
            reason = f"EDGAR insider sales ${sell_value:,.0f}"
        elif sell_value >= 500_000:
            raw_score, conf = -1.5, 0.70
            reason = f"EDGAR insider sales ${sell_value:,.0f}"
        else:
            raw_score, conf = 0.5, 0.50
            reason = f"EDGAR insider activity ${buy_value + sell_value:,.0f}"
    elif total_filings >= 5:
        raw_score, conf = 2.0, 0.75
        reason = f"{total_filings} EDGAR Form 4 filings in window (cluster)"
    elif total_filings >= 3:
        raw_score, conf = 1.5, 0.65
        reason = f"{total_filings} EDGAR Form 4 filings in window"
    elif total_filings >= 1:
        raw_score, conf = 0.8, 0.55
        reason = f"{total_filings} EDGAR Form 4 filing(s) detected"
    else:
        return  # No filings

    # Cluster boost: 3+ distinct insiders filing in window
    if len(insiders_buying) >= 3 and raw_score > 0:
        raw_score = min(raw_score + 0.5, 3.0)
        reason += f" (cluster: {len(insiders_buying)} insiders)"

    signals.append(
        Signal(
            source="edgar",
            type="insider_activity",
            score=normalize_score(raw_score, -3.0, 3.0),
            confidence=conf,
            reason=reason,
            raw=raw_data,
        )
    )
