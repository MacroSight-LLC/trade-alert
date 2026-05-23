"""SEC EDGAR MCP — free public API integration.

Tools: insider_filings, recent_filings
Requires: EDGAR_USER_AGENT env var (e.g. "trade-alert user@example.com").

SEC EDGAR is free with no API key.  Rate limit: 10 req/sec.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVICE_NAME = "SEC EDGAR MCP"

_USER_AGENT: str = os.getenv("EDGAR_USER_AGENT", "trade-alert admin@localhost")
if _USER_AGENT == "trade-alert admin@localhost":
    logger.warning(
        "EDGAR_USER_AGENT not set — using default '%s'. "
        "SEC may rate-limit requests without a valid contact email.",
        _USER_AGENT,
    )
_BASE_URL = "https://efts.sec.gov/LATEST"
_TIMEOUT = 12.0

# SEC asks for max 10 req/sec — semaphore + inter-request delay
_sem = asyncio.Semaphore(8)
_REQUEST_DELAY = 0.15


async def _get(url: str, params: dict[str, Any] | None = None) -> Any:
    """Make GET request to SEC EDGAR with rate limiting."""
    async with _sem:
        await asyncio.sleep(_REQUEST_DELAY)
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()


async def insider_filings(params: dict[str, Any]) -> dict:
    """Fetch recent Form 4 insider filings for symbols.

    Params:
        symbols: list[str] — tickers to query.
        days_back: int — lookback window (default 7).

    Returns:
        {"results": [{"symbol": str, "filings": [{"insider": str,
            "transaction_type": str, "date": str, "accession": str}, ...]}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    days_back = int(params.get("days_back", 7))

    start_dt = (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_dt = datetime.now(UTC).strftime("%Y-%m-%d")

    results: list[dict] = []
    for sym in symbols[:20]:
        try:
            data = await _get(
                f"{_BASE_URL}/search-index",
                {
                    "q": f'"{sym}"',
                    "dateRange": "custom",
                    "startdt": start_dt,
                    "enddt": end_dt,
                    "forms": "4",
                },
            )
            hits = data.get("hits", {}).get("hits", [])
            filings: list[dict] = []
            for hit in hits[:10]:
                src = hit.get("_source", {})
                display_names = src.get("display_names", [])

                # First display_name is typically the insider, second is company
                insider_name = display_names[0] if display_names else "Unknown"

                filings.append(
                    {
                        "insider": insider_name,
                        "transaction_type": "filing",
                        "date": src.get("file_date", ""),
                        "accession": src.get("adsh", ""),
                        "form": src.get("form", "4"),
                    }
                )
            results.append({"symbol": sym.upper(), "filings": filings})
        except httpx.HTTPError as exc:
            logger.warning("EDGAR insider_filings error for %s: %s", sym, exc)
            results.append({"symbol": sym.upper(), "filings": []})

    return {"results": results}


async def recent_filings(params: dict[str, Any]) -> dict:
    """Fetch recent material filings (8-K, 10-K, 10-Q) for symbols.

    Params:
        symbols: list[str] — tickers to query.
        days_back: int — lookback window (default 7).
        forms: str — comma-separated form types (default "8-K,10-K,10-Q").

    Returns:
        {"results": [{"symbol": str, "filings": [{"form_type": str,
            "date": str, "description": str}, ...]}, ...]}
    """
    symbols: list[str] = params.get("symbols", [])
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",")]
    days_back = int(params.get("days_back", 7))
    forms = params.get("forms", "8-K,10-K,10-Q")

    start_dt = (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_dt = datetime.now(UTC).strftime("%Y-%m-%d")

    results: list[dict] = []
    for sym in symbols[:20]:
        try:
            data = await _get(
                f"{_BASE_URL}/search-index",
                {
                    "q": f'"{sym}"',
                    "dateRange": "custom",
                    "startdt": start_dt,
                    "enddt": end_dt,
                    "forms": forms,
                },
            )
            hits = data.get("hits", {}).get("hits", [])
            filings: list[dict] = []
            for hit in hits[:10]:
                src = hit.get("_source", {})
                display_names = src.get("display_names", [])
                filings.append(
                    {
                        "form_type": src.get("form", ""),
                        "date": src.get("file_date", ""),
                        "description": src.get("file_description", "")
                        or (display_names[0] if display_names else ""),
                        "accession": src.get("adsh", ""),
                    }
                )
            results.append({"symbol": sym.upper(), "filings": filings})
        except httpx.HTTPError as exc:
            logger.warning("EDGAR recent_filings error for %s: %s", sym, exc)
            results.append({"symbol": sym.upper(), "filings": []})

    return {"results": results}


TOOLS: dict[str, Any] = {
    "insider_filings": insider_filings,
    "recent_filings": recent_filings,
}
