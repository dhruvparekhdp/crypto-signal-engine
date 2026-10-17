"""17 Oct 2026: Delta's candle history answered 502 for every coin; after a restart every price read 0.0. Delta stays
the source (live money is on Delta) and Binance fills in only when Delta fails."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from analysis import swing_book as sb
from config.settings import settings


class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("502", request=None, response=None)

    def json(self):
        return self._body


def _klines(n=300, now_ms=1_792_000_000_000):
    out = []
    for i in range(n):
        t = now_ms - (n - i + 1) * 4 * 3_600_000
        out.append([t, "1", "1.1", "0.9", "1.05", "100", t + 4 * 3_600_000 - 1, "0", 0, "0", "0", "0"])
    return out


@pytest.mark.asyncio
async def test_swing_bars_fall_back_to_binance_when_delta_502s(monkeypatch):
    monkeypatch.setattr(settings, "delta_only_mode", True)
    calls = []

    class Client:
        async def get(self, url, params=None, headers=None, timeout=None):
            calls.append(url)
            if "delta" in url:
                return FakeResp(502, {})
            return FakeResp(200, _klines())
    bars = await sb.fetch_bars(Client(), "XRPUSDT", 1_792_000_000_000, "4h")
    assert bars is not None and len(bars.c) > 100
    assert "delta" in calls[0] and "fapi.binance.com" in calls[1]


@pytest.mark.asyncio
async def test_delta_is_used_when_it_works(monkeypatch):
    monkeypatch.setattr(settings, "delta_only_mode", True)
    now = 1_792_000_000_000
    rows = [{"time": (now - (300 - i + 1) * 4 * 3_600_000) // 1000, "open": 1, "high": 1.1, "low": 0.9, "close": 1.05,
             "volume": 10} for i in range(300)]
    calls = []

    class Client:
        async def get(self, url, params=None, headers=None, timeout=None):
            calls.append(url)
            return FakeResp(200, {"result": rows})
    bars = await sb.fetch_bars(Client(), "XRPUSDT", now, "4h")
    assert bars is not None and len(bars.c) > 100 and all("delta" in u for u in calls)


@pytest.mark.asyncio
async def test_klines_job_fills_coins_delta_missed_from_binance():
    from scheduler.runner import AppRunner
    r = AppRunner.__new__(AppRunner)
    r.delta_market = SimpleNamespace(refreshed={"btcusdt"}, last_error="XRPUSD: HTTP 502")
    r.crypto_store = SimpleNamespace(get_symbols=AsyncMock(return_value=["btcusdt", "xrpusdt"]), replace_candles=AsyncMock())
    bars = [{"timestamp": i, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1} for i in range(50)]
    r.klines = SimpleNamespace(fetch_candles=AsyncMock(return_value=bars))
    await r._binance_fallback_candles()
    r.klines.fetch_candles.assert_awaited_once_with("xrpusdt")
    r.crypto_store.replace_candles.assert_awaited_once_with("xrpusdt", bars)
