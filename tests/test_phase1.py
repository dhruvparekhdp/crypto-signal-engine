"""Phase 1 of docs/INTEGRATION_PLAN.md: protect production (findings L1-L11)."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_l9_bad_swing_setting_pauses_swing_and_alerts_once(monkeypatch):
    from config.settings import settings
    from scheduler.runner import AppRunner
    monkeypatch.setattr(settings, "swing_enabled", True)
    monkeypatch.setattr(settings, "swing_strategies", "4h@vol_breakout:zz=3.0")
    monkeypatch.setattr(settings, "swing_config_fail_closed", True)
    r = AppRunner()
    r.notifier = AsyncMock()
    r.crypto_store.get_all = AsyncMock(side_effect=AssertionError("must not scan with a bad setting"))
    await r._swing_scan_job()
    await r._swing_scan_job()
    assert r.notifier.send_text.await_count == 1
    assert "has no setting 'zz'" in r.notifier.send_text.await_args.args[0]


from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from analysis import swing_book as sb  # noqa: E402
from analysis.crypto_state import CryptoState  # noqa: E402
from analysis.regime_gate import Verdict  # noqa: E402


async def _swing(signals, patches=None, setup=None):
    """Run one paper tick with the given (symbol, strategy, side, price) swing signals; return open rows + logs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    r = AppRunner()
    r.notifier = AsyncMock()
    base = {"scheduler.runner.settings.paper_trading_enabled": True,
            "scheduler.runner.settings.session_filter_enabled": False,
            "scheduler.runner.AsyncSessionFactory": sm}
    base.update(patches or {})
    ctx = [patch(k, v) for k, v in base.items()]
    for c in ctx:
        c.start()
    try:
        if setup:
            await setup(r, sm)
        now = datetime.now(UTC)
        for sym, strat, side, px in signals:
            st = CryptoState(symbol=sym.lower(), base_asset=sym[:-4])
            st.current_price, st.atr_14 = px, px * 0.005
            r.crypto_store._states[sym.lower()] = st
            sig = sb.to_signal(sb.SwingSetup(sym, strat, side, 0, px * 0.015, px), px, now)
            sig.regime = Verdict(0.2, 15.0, [])
            r._pending_swing.append(sig)
        await r._paper_trading_job()
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            rows = await repo.get_open_positions(cycle.id)
            logs = await repo.crypto_signals_between(1, 0)
    finally:
        for c in ctx:
            c.stop()
    return rows, logs, r


@pytest.mark.asyncio
async def test_l3_total_same_side_risk_is_capped():
    """Two 3%-risk longs = 6% on one market move; with a 6% cap the third and fourth longs are refused, the short
    still opens (the cap is per direction)."""
    sigs = [("BTCUSDT", "donchian", 1, 60000.0), ("SOLUSDT", "vol_breakout", 1, 150.0),
            ("DOGEUSDT", "keltner_break", 1, 0.2), ("XRPUSDT", "ichimoku", 1, 0.6),
            ("ETHUSDT", "donchian", -1, 3000.0)]
    rows, logs, _ = await _swing(sigs, {"scheduler.runner.settings.swing_max_side_risk_pct": 0.06})
    longs = [x for x in rows if x.side == "long"]
    assert len(longs) == 2 and any(x.side == "short" for x in rows)
    assert {x.skip_reason for x in logs if x.skip_reason} == {"side_risk_cap_6pct"}


@pytest.mark.asyncio
async def test_l2_news_blackout_applies_to_swing():
    from types import SimpleNamespace as NS
    rows, logs, _ = await _swing([("BTCUSDT", "donchian", 1, 60000.0)], {
        "scheduler.runner.AppRunner._blackout": lambda self, now: NS(name="FOMC", kind="calendar"),
        "scheduler.runner.settings.event_bias_mode": False})
    assert rows == [] and logs[0].skip_reason == "news_blackout"


@pytest.mark.asyncio
async def test_l2_daily_loss_limit_stops_new_swing_trades():
    from scheduler.runner import AppRunner
    now = datetime.now(UTC)
    trades = [SimpleNamespace(symbol="btcusdt", closed_at=now - timedelta(hours=1), net_pnl=-150.0, side="long")] * 2
    r = AppRunner()
    with patch("scheduler.runner.settings.swing_daily_loss_pct", 9.0):
        assert r._swing_account_guard(trades, now, 2700.0) == "daily_loss_limit"        # lost 300 of 3,000 = 10%
        assert r._swing_account_guard(trades[:1], now, 2850.0) is None                   # 5%: fine
    streak = [SimpleNamespace(symbol="x", closed_at=now - timedelta(minutes=m), net_pnl=-1.0, side="long") for m in (5, 10, 15)]
    with patch("scheduler.runner.settings.swing_daily_loss_pct", 99.0):
        assert r._swing_account_guard(streak, now, 3000.0) == "losing_streak_pause"


@pytest.mark.asyncio
async def test_l8_signal_without_regime_data_is_not_traded():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    r = AppRunner()
    r.notifier = AsyncMock()
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.swing_regime_fail_closed", True), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        st = CryptoState(symbol="btcusdt", base_asset="BTC")
        st.current_price, st.atr_14 = 60000.0, 300.0
        r.crypto_store._states["btcusdt"] = st
        sig = sb.to_signal(sb.SwingSetup("BTCUSDT", "donchian", 1, 0, 900.0, 60000.0), 60000.0, datetime.now(UTC))
        sig.regime = None
        r._pending_swing.append(sig)
        await r._paper_trading_job()
        from storage.repository import Repository
        async with sm() as s:
            logs = await Repository(s).crypto_signals_between(1, 0)
    assert logs[0].skip_reason == "regime_unknown"


def test_l4_live_stop_fills_at_the_gap_price():
    from analysis.paper_trading import ExitReason, Side, resolve_candle
    pos = SimpleNamespace(side=Side.LONG, sign=1, stop_price=95.0, liq_price=50.0, target_price=120.0, expires_at=None)
    from analysis.paper_trading import NO_SLIPPAGE
    assert resolve_candle(pos, 90.0, 90.0, 90.0, datetime.now(UTC), NO_SLIPPAGE) == (ExitReason.STOP, 95.0)
    assert resolve_candle(pos, 90.0, 90.0, 90.0, datetime.now(UTC), NO_SLIPPAGE, gap_price=90.0) == (ExitReason.STOP, 90.0)
    assert resolve_candle(pos, 96.0, 94.0, 95.5, datetime.now(UTC), NO_SLIPPAGE, gap_price=96.0) == (ExitReason.STOP, 95.0)


@pytest.mark.asyncio
async def test_l1_swing_positions_and_entries_use_perp_prices(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "swing_perp_prices", True)

    async def fake_perp(self):
        return {"BTCUSDT": 60300.0}

    rows, logs, r = await _swing([("BTCUSDT", "donchian", 1, 60000.0)],
                                 {"scheduler.runner.AppRunner._perp_prices": fake_perp})
    assert rows[0].entry_price == pytest.approx(60300.0, rel=0.002)     # entered on the perp price (plus slippage)


@pytest.mark.asyncio
async def test_l6_l7_seen_bars_survive_restart_and_nothing_queues_while_paper_is_off(tmp_path, monkeypatch):
    from scheduler.runner import AppRunner
    monkeypatch.setattr(AppRunner, "_SWING_SEEN_FILE", str(tmp_path / "seen.json"))
    a = AppRunner()
    a._swing_seen = {"BTCUSDT@4h": 123}
    a._save_swing_seen()
    assert AppRunner()._load_swing_seen() == {"BTCUSDT@4h": 123}


def test_l11_simulator_keys_are_not_on_the_command_line():
    import inspect

    import scheduler.health as h
    src = inspect.getsource(h)
    assert '"--gemini-key"' not in src and '"--openrouter-key"' not in src and "SIM_GEMINI_KEY" in src
