"""Structured gate rejection reasons for telemetry."""

from __future__ import annotations

from enum import Enum


class GateRejection(str, Enum):
    """Structured gate rejection reasons for telemetry."""

    VIX_HARD = "vix_hard"
    SOURCE_HALLUCINATION = "source_hallucination"
    EP_THRESHOLD = "ep_threshold"
    SA_THRESHOLD = "sa_threshold"
    HIGH_CONFIDENCE_ALIGNMENT = "high_confidence_alignment"
    CONF_THRESHOLD = "conf_threshold"
    RR_MINIMUM = "rr_minimum"
    RR_ZERO_RISK = "rr_zero_risk"
    ENTRY_ORDER_INVALID = "entry_order_invalid"
    MACRO_VETO = "macro_veto"
    VIX_SOFT = "vix_soft"
    FORECAST_CONTRADICTS = "forecast_contradicts"
    TIMEFRAME_INVALID = "timeframe_invalid"
    ENTRY_MARKET_DRIFT = "entry_market_drift"
    VOLUME_UNCONFIRMED = "volume_unconfirmed"
    WATCH_EP_THRESHOLD = "watch_ep_threshold"
    WATCH_SA_THRESHOLD = "watch_sa_threshold"
    WATCH_CONF_THRESHOLD = "watch_conf_threshold"
    WATCH_CAP = "watch_cap"
    WATCH_DROPPED_DIRECTIONAL_PRESENT = "watch_dropped_directional_present"
    WATCH_DECAY = "watch_decay"
    MARKET_SESSION_CLOSED = "market_session_closed"
    DEDUP_SUPPRESSED = "dedup_suppressed"
