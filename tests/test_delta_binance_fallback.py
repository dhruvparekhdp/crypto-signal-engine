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
    assert bars is not None and len(bars.c) > 100 and "delta" in calls[0]
    assert all("delta" in u for u in calls[1:] if "fapi.binance.com" not in u)      # prices: Delta only
    assert sum("fapi.binance.com" in u for u in calls) <= 1                          # one volume read (18 Oct)


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


# ---- 18 Oct 2026 review: the last jobs that still called Binance first ------------------------------------------
def _delta_bars(n, step_s, end):
    from datetime import timedelta
    return [{"timestamp": end - timedelta(seconds=step_s * (n - i)), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05,
             "volume": 5.0} for i in range(n)]


def test_delta_rows_for_store_keeps_closed_bars_and_drops_bad_prices():
    from datetime import UTC, datetime, timedelta

    from scheduler.runner import delta_rows_for_store
    now = datetime(2026, 10, 18, 12, 30, tzinfo=UTC)
    bars = _delta_bars(3, 3600, datetime(2026, 10, 18, 12, 0, tzinfo=UTC))      # 09:00, 10:00, 11:00 opens
    bars.append({"timestamp": datetime(2026, 10, 18, 12, 0, tzinfo=UTC), "open": 1, "high": 1, "low": 1, "close": 1})
    bars[0]["low"] = 0.0                                                        # a broken print
    rows = delta_rows_for_store(bars, "XRPUSDT", "1h", now)
    assert [r["open_time"].hour for r in rows] == [10, 11]                       # 12:00 still forming, 09:00 broken
    assert rows[0]["source"] == "delta" and rows[0]["symbol"] == "xrpusdt" and rows[0]["open_time"].tzinfo is None
    assert rows[1]["open_time"] + timedelta(hours=1) <= now.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_candle_refresh_saves_delta_bars_and_skips_binance(monkeypatch):
    from datetime import UTC, datetime

    from scheduler import runner as R
    r = R.AppRunner.__new__(R.AppRunner)
    now = datetime.now(UTC)
    r.crypto_store = SimpleNamespace(get_all=AsyncMock(return_value=[SimpleNamespace(symbol="xrpusdt")]))
    r.delta_market = SimpleNamespace(fetch_candles=AsyncMock(side_effect=lambda s, i, limit: _delta_bars(
        limit, 300 if i == "5m" else 3600, now.replace(second=0, microsecond=0, minute=0))))
    saved = []

    class Repo:
        def __init__(self, s):
            pass

        async def save_candles(self, rows):
            saved.extend(rows)
            return len(rows)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class NoBinance:
        def fetch_range(self, *a, **k):
            raise AssertionError("Binance called although Delta answered")
    monkeypatch.setattr(R, "Repository", Repo)
    monkeypatch.setattr(R, "AsyncSessionFactory", Session)
    monkeypatch.setattr("collectors.binance_history.BinanceHistory", NoBinance)
    await r._candle_refresh_job()
    assert saved and {x["source"] for x in saved} == {"delta"} and {x["interval"] for x in saved} == {"5m", "1h"}


def test_a_stale_binance_only_flag_does_not_switch_feeds_off_in_delta_only_mode(monkeypatch):
    from scheduler.runner import _binance_only
    monkeypatch.setattr(settings, "binance_only_mode", True)
    monkeypatch.setattr(settings, "delta_only_mode", True)
    assert _binance_only() is False                  # production had both saved: news + Fear & Greed stayed off
    monkeypatch.setattr(settings, "delta_only_mode", False)
    assert _binance_only() is True


@pytest.mark.asyncio
async def test_swing_outcomes_replay_on_delta_bars(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from scheduler import runner as R
    r = R.AppRunner.__new__(R.AppRunner)
    t0 = (datetime.now(UTC) - timedelta(hours=10)).replace(minute=0, second=0, microsecond=0)
    sig = SimpleNamespace(id=7, outcome="pending", timestamp=t0.replace(tzinfo=None), symbol="xrpusdt", direction="long",
                          current_price=1.0, stop_loss=0.95, target_price=1.15, signal_type="swing_donchian")
    bars = [{"timestamp": t0 + timedelta(hours=i), "open": 1.0, "high": 1.2 if i == 3 else 1.01, "low": 0.99,
             "close": 1.0, "volume": 1.0} for i in range(12)]
    r.delta_market = SimpleNamespace(fetch_candles=AsyncMock(return_value=bars))
    resolved = []

    class Repo:
        def __init__(self, s):
            pass

        async def swing_signals_since(self, days):
            return [sig]

        async def resolve_crypto_signal(self, sid, outcome, pct):
            resolved.append((sid, outcome, pct))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def no_binance(*a, **k):
        raise AssertionError("Binance called although Delta answered")
    monkeypatch.setattr(R, "Repository", Repo)
    monkeypatch.setattr(R, "AsyncSessionFactory", Session)
    monkeypatch.setattr(httpx.AsyncClient, "get", no_binance)
    await r._swing_outcomes_job()
    assert resolved and resolved[0][:2] == (7, "won")             # the 3R target was hit on the 4th Delta bar


def test_8h_bars_from_newest_first_delta_rows_keep_open_and_close_in_order():
    """Delta's /v2/history/candles answers newest first; an 8h bar's open is its first 4h open, its close the last."""
    from analysis.swing_book import _aggregate_delta_rows
    t0 = 1_792_000_000 // 28_800 * 28_800                                   # an 8h boundary (UTC 00/08/16)
    rows = [{"time": t0 + 14_400, "open": 105, "high": 112, "low": 104, "close": 110, "volume": 2},   # 2nd half
            {"time": t0, "open": 100, "high": 106, "low": 98, "close": 105, "volume": 1}]           # 1st half
    bar = _aggregate_delta_rows(rows, 14_400, 28_800)[0]
    assert (bar["time"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (t0, 100, 112, 98, 110, 3)


@pytest.mark.asyncio
async def test_delta_bars_carry_binance_volume_for_the_volume_strategy(monkeypatch):
    """vol_breakout was backtested on Binance volume; on Delta's thin volume it fired on the same bar only 36-41% of
    the time (18 Oct 2026). Prices stay Delta's; the volume column comes from Binance when it covers every bar."""
    monkeypatch.setattr(settings, "delta_only_mode", True)
    now = 1_792_000_000_000
    step = 4 * 3_600_000
    opens = [now - (300 - i + 1) * step for i in range(300)]
    delta_rows = [{"time": t // 1000, "open": 1, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 7} for t in opens]
    binance = [[t, "1", "1.1", "0.9", "1.04", str(1000 + i), t + step - 1, "0", 0, "0", "0", "0"]
               for i, t in enumerate(opens + [now - step])]

    class Client:
        def __init__(self, binance_ok=True):
            self.binance_ok = binance_ok

        async def get(self, url, params=None, headers=None, timeout=None):
            if "delta" in url:
                return FakeResp(200, {"result": list(reversed(delta_rows))})      # Delta answers newest first
            return FakeResp(200, binance) if self.binance_ok else FakeResp(502, {})
    bars = await sb.fetch_bars(Client(), "XRPUSDT", now, "4h")
    assert bars.c[-1] == 1.05                                  # Delta's price
    assert list(bars.v[-3:]) == [1297.0, 1298.0, 1299.0]       # Binance's volume, matched by bar open time
    bars = await sb.fetch_bars(Client(binance_ok=False), "XRPUSDT", now, "4h")
    assert set(bars.v) == {7.0}                                # Binance down: Delta volume, whole


@pytest.mark.asyncio
async def test_daily_trend_reads_delta_daily_closes():
    from datetime import UTC, datetime, timedelta

    from scheduler.runner import AppRunner
    r = AppRunner.__new__(AppRunner)
    day0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    bars = [{"timestamp": day0 - timedelta(days=30 - i), "open": 1, "high": 1, "low": 1, "close": float(i), "volume": 1}
            for i in range(30)] + [{"timestamp": day0, "open": 1, "high": 1, "low": 1, "close": 999.0, "volume": 1}]
    st = SimpleNamespace(symbol="xrpusdt", daily_sma20=None, daily_trend_at=None)
    r.crypto_store = SimpleNamespace(get_all=AsyncMock(return_value=[st]))
    r.delta_market = SimpleNamespace(fetch_candles=AsyncMock(return_value=bars))
    r.klines = SimpleNamespace(FETCH_CONCURRENCY=5, fetch_candles=AsyncMock(side_effect=AssertionError("Binance")))
    await r._daily_trend_job()
    assert st.daily_sma20 == pytest.approx(sum(range(10, 30)) / 20)       # last 20 closed days; today's 999 ignored
