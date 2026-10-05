"""Regime check for swing signals: same numbers as the backtest, shadow mode trades everything, "on" skips."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis import regime_gate as rg
from analysis import swing_book as sb
from analysis.crypto_state import CryptoState
from storage.models import Base

LAKE = Path("data/lake/um/klines/BTCUSDT/1m")


def walk(n, vol, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    return c


class TestVerdict:
    def test_wild_btc_volatility_is_flagged(self):
        calm, wild = walk(400, 0.01, 1), walk(30, 0.08, 2)
        btc = np.concatenate([calm, calm[-1] * wild / wild[0]])     # last 30 days far wilder than the past year
        c = walk(300, 0.02, 3)
        v = rg.judge(btc, c * 1.01, c * 0.99, c)
        assert v.btc_vol_rank > 0.9 and "wild_market" in v.reasons and v.would_skip

    def test_calm_market_and_no_trend_is_taken(self):
        wild, calm = walk(400, 0.05, 4), walk(30, 0.005, 5)
        btc = np.concatenate([wild, wild[-1] * calm / calm[0]])
        rng = np.random.default_rng(6)
        c = 100 + rng.normal(0, 1, 300)                               # range-bound coin: low ADX
        v = rg.judge(btc, c + 1, c - 1, c)
        assert v.btc_vol_rank < 0.2 and v.coin_adx < 30 and not v.would_skip

    def test_strong_trend_is_flagged(self):
        c = 100 * np.exp(np.linspace(0, 1.5, 300))                   # one-way climb: ADX near its maximum
        v = rg.judge(None, c * 1.005, c * 0.995, c)
        assert v.coin_adx > 30 and v.reasons == ["strong_trend"]

    def test_missing_history_never_skips(self):
        v = rg.judge(np.ones(50), None, None, None)
        assert v.btc_vol_rank is None and v.coin_adx is None and not v.would_skip

    def test_tag_round_trips(self):
        v = rg.Verdict(0.812, 34.25, ["wild_market", "strong_trend"])
        assert rg.parse_tag("atr4h=1 | " + v.tag()) == {"btc_vol_rank": 0.81, "coin_adx": 34.2, "would_skip": True,
                                                       "reasons": ["wild_market", "strong_trend"]}
        assert rg.parse_tag(rg.Verdict(None, 12.0, []).tag())["would_skip"] is False
        assert rg.parse_tag("no tag here") is None


@pytest.mark.skipif(not LAKE.exists(), reason="lake not available")
def test_live_check_matches_the_backtest_series():
    """The verdict on the last closed bar must equal the backtest's series value for that bar."""
    from analysis.lab.data import load_bars
    btc, sol = load_bars("BTCUSDT", "1d"), load_bars("SOLUSDT", "1d")
    rank, adx = rg.vol_rank_series(btc.c), rg.adx_series(sol.h, sol.l, sol.c)
    for i in (600, 900, len(sol) - 1):
        w = slice(i - 499, i + 1)                                    # live fetches the last 500 daily bars
        v = rg.judge(btc.c[w] if i < len(btc) else None, sol.h[w], sol.l[w], sol.c[w])
        assert v.btc_vol_rank == pytest.approx(rank[i])
        assert v.coin_adx == pytest.approx(adx[i], rel=1e-3)          # ADX smoothing has long forgotten its start


def test_scoreboard_splits_trades_by_their_signals_verdict():
    t0 = datetime(2026, 10, 5, 8, tzinfo=UTC)
    sig = lambda sym, v: {"symbol": sym, "signal_type": "swing_donchian", "direction": "long", "timestamp": t0,
                          "indicators_summary": "atr=1 | " + v.tag()}
    signals = [sig("btcusdt", rg.Verdict(0.2, 15.0, [])), sig("ethusdt", rg.Verdict(0.9, 15.0, ["wild_market"]))]
    trade = lambda sym, r, mins=2: {"symbol": sym, "signal_type": "swing_donchian", "side": "long", "r": r,
                                    "opened_at": t0 + timedelta(minutes=mins)}
    out = rg.shadow_scoreboard(signals, [trade("btcusdt", 2.9), trade("ethusdt", -1.0), trade("solusdt", 1.0),
                                         trade("btcusdt", -1.0, mins=300)])
    assert out["take"]["n"] == 1 and out["take"]["avg_r"] == pytest.approx(2.9)
    assert out["skip"]["n"] == 1 and out["by_reason"]["wild_market"]["total_r"] == pytest.approx(-1.0)
    assert out["untagged"]["n"] == 2                                  # no signal, or opened long after it


async def _run(mode: str, would_skip: bool):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    from scheduler.runner import AppRunner
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.settings.swing_regime_filter", mode), \
         patch("scheduler.runner.AsyncSessionFactory", session_maker):
        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price = 60000.0
        st.atr_14 = 300.0
        runner.crypto_store._states["btcusdt"] = st
        sig = sb.to_signal(sb.SwingSetup("BTCUSDT", "keltner_break", +1, 0, 1000.0, 60000.0), 60000.0, datetime.now(UTC))
        v = rg.Verdict(0.9 if would_skip else 0.2, 20.0, ["wild_market"] if would_skip else [])
        sig.regime = v
        sig.indicators_summary += " | " + v.tag()
        runner._pending_swing.append(sig)
        await runner._paper_trading_job()
        from storage.repository import Repository
        async with session_maker() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            rows = await repo.get_open_positions(cycle.id)
            logs = await repo.crypto_signals_between(1, 0)
    return rows, logs


@pytest.mark.asyncio
async def test_shadow_mode_still_opens_a_would_skip_signal_and_logs_the_verdict():
    rows, logs = await _run("shadow", would_skip=True)
    assert len(rows) == 1
    assert rg.parse_tag(logs[0].indicators_summary)["reasons"] == ["wild_market"]
    assert not logs[0].skip_reason


@pytest.mark.asyncio
async def test_on_mode_skips_it_and_says_why():
    rows, logs = await _run("on", would_skip=True)
    assert rows == []
    assert logs[0].skip_reason == "regime_wild_market"


@pytest.mark.asyncio
async def test_on_mode_still_takes_a_calm_signal():
    rows, _ = await _run("on", would_skip=False)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_live_verdict_fetches_daily_bars_once_per_day_and_survives_failures():
    from analysis.lab.data import Bars
    from scheduler.runner import AppRunner
    c = walk(500, 0.02, 7)
    bars = Bars("X", "1d", np.arange(500, dtype=np.int64) * 86_400_000, c, c * 1.01, c * 0.99, c, np.ones(500), np.ones(500))
    calls = []

    async def fake_fetch(client, symbol, now_ms, tf="4h", limit=500):
        calls.append((symbol, tf))
        return bars

    runner = AppRunner()
    now_ms = 1_791_000_000_000
    with patch("analysis.swing_book.fetch_bars", fake_fetch), patch("scheduler.runner.settings.swing_regime_filter", "shadow"):
        v1 = await runner._regime_verdict(None, "SOLUSDT", now_ms)
        v2 = await runner._regime_verdict(None, "SOLUSDT", now_ms + 3_600_000)
    assert v1 is not None and v1.as_dict() == v2.as_dict()
    assert calls == [("BTCUSDT", "1d"), ("SOLUSDT", "1d")]           # second call served from the day cache

    async def broken(*a, **k):
        raise RuntimeError("binance down")

    runner2 = AppRunner()
    with patch("analysis.swing_book.fetch_bars", broken), patch("scheduler.runner.settings.swing_regime_filter", "shadow"):
        assert await runner2._regime_verdict(None, "SOLUSDT", now_ms) is None
    with patch("scheduler.runner.settings.swing_regime_filter", "off"):
        assert await runner2._regime_verdict(None, "SOLUSDT", now_ms) is None
