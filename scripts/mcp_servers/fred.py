"""FRED bundle MCP — real API integration.

Tools: vix_level, yield_curve, fed_funds
Requires: FRED_API_KEY env var.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "FRED bundle MCP"

API_KEY: str = os.getenv("FRED_API_KEY", "")
BASE_URL = "https://api.stlouisfed.org/fred"
TIMEOUT = 10.0

# FRED series IDs
VIXCLS = "VIXCLS"  # VIX close
DGS10 = "DGS10"    # 10-year Treasury
DGS2 = "DGS2"      # 2-year Treasury
FEDFUNDS = "FEDFUNDS"  # Fed funds rate


async def _latest_value(series_id: str) -> float | None:
    """Fetch the most recent observation for a FRED series."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/series/observations",
            params={
                "series_id": series_id,
                "api_key": API_KEY,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 5,
            },
        )
        resp.raise_for_status()
        observations = resp.json().get("observations", [])
        for obs in observations:
            val = obs.get("value", ".")
            if val != ".":
                return float(val)
    return None


async def vix_level(params: dict[str, Any]) -> dict:
    """Fetch current VIX level from FRED.

    Returns:
        {"value": float, "vix_level": float}
    """
    vix = await _latest_value(VIXCLS)
    if vix is None:
        return {"value": 0.0, "vix_level": 0.0, "error": "no data"}
    return {"value": vix, "vix_level": vix}


async def yield_curve(params: dict[str, Any]) -> dict:
    """Fetch 10Y-2Y yield curve spread from FRED.

    Returns:
        {"value": float, "spread_bps": float}
    """
    dgs10 = await _latest_value(DGS10)
    dgs2 = await _latest_value(DGS2)
    if dgs10 is None or dgs2 is None:
        return {"value": 0.0, "spread_bps": 0.0, "error": "no data"}
    spread_bps = round((dgs10 - dgs2) * 100, 1)
    return {"value": spread_bps, "spread_bps": spread_bps}


async def fed_funds(params: dict[str, Any]) -> dict:
    """Fetch current federal funds rate from FRED.

    Returns:
        {"value": float, "rate": float}
    """
    rate = await _latest_value(FEDFUNDS)
    if rate is None:
        return {"value": 0.0, "rate": 0.0, "error": "no data"}
    return {"value": rate, "rate": rate}


TOOLS: dict[str, Any] = {
    "vix_level": vix_level,
    "yield_curve": yield_curve,
    "fed_funds": fed_funds,
}
