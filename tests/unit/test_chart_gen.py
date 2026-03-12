"""Tests for chart_gen module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from chart_gen import _fetch_candles, generate_chart

# ── Sample OHLCV response ──────────────────────────────────────


def _make_polygon_bars(n: int = 20) -> list[dict]:
    """Return *n* fake Polygon OHLCV bar dicts."""
    bars = []
    base_ts = 1_700_000_000_000  # arbitrary ms epoch
    for i in range(n):
        bars.append(
            {
                "o": 100.0 + i,
                "h": 102.0 + i,
                "l": 98.0 + i,
                "c": 101.0 + i,
                "v": 1_000_000 + i * 10_000,
                "t": base_ts + i * 60_000 * 15,
            }
        )
    return bars


def _polygon_json(n: int = 20) -> dict:
    return {"results": _make_polygon_bars(n), "resultsCount": n}


# ── _fetch_candles ──────────────────────────────────────────────


class TestFetchCandles:
    """Tests for the _fetch_candles helper."""

    @patch.dict("os.environ", {"POLYGON_API_KEY": "test-key"})
    @patch("chart_gen._get_chart_client")
    def test_returns_dataframe(self, mock_client_fn: MagicMock) -> None:
        """Happy-path: returns properly-shaped DataFrame."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _polygon_json(20)
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_client_fn.return_value = MagicMock(get=MagicMock(return_value=mock_resp))

        df = _fetch_candles("NVDA", "15m")

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 20
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.name == "Date"

    @patch.dict("os.environ", {"POLYGON_API_KEY": "test-key"})
    @patch("chart_gen._get_chart_client")
    def test_empty_on_no_results(self, mock_client_fn: MagicMock) -> None:
        """Returns empty DataFrame when Polygon has no data."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_client_fn.return_value = MagicMock(get=MagicMock(return_value=mock_resp))

        df = _fetch_candles("FAKE", "1h")
        assert df.empty

    @patch.dict("os.environ", {"POLYGON_API_KEY": "test-key"})
    @patch("chart_gen._get_chart_client")
    def test_empty_on_http_error(self, mock_client_fn: MagicMock) -> None:
        """Returns empty DataFrame when the HTTP request fails."""
        import httpx as _httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "500",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )
        mock_client_fn.return_value = MagicMock(get=MagicMock(return_value=mock_resp))

        df = _fetch_candles("NVDA", "15m")
        assert df.empty

    @patch.dict("os.environ", {"POLYGON_API_KEY": "test-key"})
    @patch("chart_gen._get_chart_client")
    def test_unknown_timeframe_uses_default(self, mock_client_fn: MagicMock) -> None:
        """Unknown timeframe falls back to 15m/minute/48."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _polygon_json(10)
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_client_fn.return_value = MagicMock(get=MagicMock(return_value=mock_resp))

        df = _fetch_candles("AAPL", "unknown")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 10

    @patch.dict("os.environ", {}, clear=True)
    def test_empty_on_missing_api_key(self) -> None:
        """Returns empty DataFrame immediately when POLYGON_API_KEY is unset."""
        df = _fetch_candles("NVDA", "15m")
        assert df.empty

    @patch("chart_gen.time.sleep")
    @patch.dict("os.environ", {"POLYGON_API_KEY": "test-key"})
    @patch("chart_gen._get_chart_client")
    def test_retries_on_429(self, mock_client_fn: MagicMock, mock_sleep: MagicMock) -> None:
        """Retries once on 429 with backoff, then succeeds."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = _polygon_json(10)
        resp_ok.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = MagicMock(side_effect=[resp_429, resp_ok])
        mock_client_fn.return_value = mock_client

        df = _fetch_candles("NVDA", "15m")
        assert not df.empty
        assert len(df) == 10
        mock_sleep.assert_called_once()


# ── generate_chart ──────────────────────────────────────────────


class TestGenerateChart:
    """Tests for the public generate_chart function."""

    @patch("chart_gen._fetch_candles")
    def test_returns_png_bytes(self, mock_fetch: MagicMock) -> None:
        """Happy-path: returns valid PNG bytes."""
        bars = _make_polygon_bars(30)
        df = pd.DataFrame(bars).rename(
            columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "ts"},
        )
        df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
        mock_fetch.return_value = df

        result = generate_chart("NVDA", "15m", {"level": 110.0, "stop": 105.0, "target": 120.0})

        assert result is not None
        # PNG magic bytes
        assert result[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(result) > 1000  # sanity: a real chart should be >1KB

    @patch("chart_gen._fetch_candles")
    def test_returns_none_on_empty_data(self, mock_fetch: MagicMock) -> None:
        """Returns None when no candle data is available."""
        mock_fetch.return_value = pd.DataFrame()

        result = generate_chart("FAKE", "1h", {"level": 50.0, "stop": 48.0, "target": 55.0})
        assert result is None

    @patch("chart_gen._fetch_candles")
    def test_handles_zero_prices(self, mock_fetch: MagicMock) -> None:
        """Zero entry prices should be skipped gracefully (no crash)."""
        bars = _make_polygon_bars(20)
        df = pd.DataFrame(bars).rename(
            columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "ts"},
        )
        df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
        mock_fetch.return_value = df

        result = generate_chart("NVDA", "15m", {"level": 0, "stop": 0, "target": 0})
        # Should still produce a chart, just without overlay lines
        assert result is not None
        assert result[:8] == b"\x89PNG\r\n\x1a\n"

    @patch("chart_gen._fetch_candles")
    def test_handles_missing_entry_keys(self, mock_fetch: MagicMock) -> None:
        """Missing entry dict keys default to 0 (no crash)."""
        bars = _make_polygon_bars(20)
        df = pd.DataFrame(bars).rename(
            columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "ts"},
        )
        df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
        mock_fetch.return_value = df

        result = generate_chart("NVDA", "15m", {})
        assert result is not None

    @patch("chart_gen._fetch_candles")
    def test_returns_none_when_mplfinance_missing(self, mock_fetch: MagicMock) -> None:
        """Returns None if mplfinance cannot be imported."""
        bars = _make_polygon_bars(20)
        df = pd.DataFrame(bars).rename(
            columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "ts"},
        )
        df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
        mock_fetch.return_value = df

        import builtins

        real_import = builtins.__import__

        def mock_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "mplfinance":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = generate_chart("NVDA", "15m", {"level": 110.0, "stop": 105.0, "target": 120.0})
        assert result is None

    @patch("chart_gen._fetch_candles")
    def test_daily_timeframe(self, mock_fetch: MagicMock) -> None:
        """1D timeframe uses day span and renders correctly."""
        bars = _make_polygon_bars(40)
        df = pd.DataFrame(bars).rename(
            columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "t": "ts"},
        )
        df["Date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
        mock_fetch.return_value = df

        result = generate_chart("SPY", "1D", {"level": 500.0, "stop": 495.0, "target": 510.0})
        assert result is not None
        assert result[:8] == b"\x89PNG\r\n\x1a\n"
