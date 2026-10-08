"""Strategy registry (plan 2.3/2.4): forward clock per spec, only incubating/trusted specs trade, drawdown alarm."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis import strategy_registry as reg
from storage.models import Base, StrategyRegistry
from tests.test_swing_selection import run

T0 = datetime(2026, 10, 1)


def _trade(r_mult, i, key=("swing_donchian", "4h")):
    # long, entry 100, stop 95 (risk 5%): exit sets the R; no costs
    return SimpleNamespace(side="long", entry_price=100.0, stop_price=95.0, exit_price=100 + 5 * r_mult,
                           trading_fees=0.0, funding_paid=0.0, margin=10.0, leverage=5.0,
                           signal_type=key[0], timeframe=key[1], opened_at=T0 + timedelta(hours=i),
                           closed_at=T0 + timedelta(hours=i, minutes=30))


async def _db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def test_trade_r_and_drawdown():
    assert reg.trade_r(_trade(2, 0)) == pytest.approx(2.0)
    assert reg.trade_r(_trade(-1, 0)) == pytest.approx(-1.0)
    assert reg.drawdown_r([1, 2, -1, -1, -1, 3]) == pytest.approx(3.0)
    assert reg.drawdown_r([1, 1]) == 0.0
    assert reg.key_of_signal("swing_vol_breakout", "4h") == "4h@vol_breakout"


@pytest.mark.asyncio
async def test_sync_registers_specs_and_a_changed_spec_restarts_its_clock():
    sm = await _db()
    async with sm() as s:
        notes = await reg.sync(s, [("4h", "donchian", {"n": 100})])
        assert notes == ["4h@donchian: registered (incubating)"]
        await reg.stamp_live_from(s, "4h@donchian", T0)
        assert (await reg.sync(s, [("4h", "donchian", {"n": 100})])) == []        # same digest: nothing changes
        assert (await reg.load(s))["4h@donchian"].live_from == T0
        notes = await reg.sync(s, [("4h", "donchian", {"n": 120})])               # params changed
        row = (await reg.load(s))["4h@donchian"]
        assert notes == ["4h@donchian: changed, live_from reset"] and row.live_from is None


@pytest.mark.asyncio
async def test_drawdown_alarm_pauses_only_past_the_specs_backtest_worst():
    sm = await _db()
    async with sm() as s:
        await reg.sync(s, [("4h", "vol_breakout", {"z": 3.0})])
        await reg.stamp_live_from(s, "4h@vol_breakout", T0)
        key = ("swing_vol_breakout", "4h")
        # backtest worst for 4h@vol_breakout is 13.8R: 13 straight losses do not trip it, 14 do
        assert await reg.check_alarms(s, [_trade(-1, i, key) for i in range(13)]) == []
        msgs = await reg.check_alarms(s, [_trade(-1, i, key) for i in range(14)])
        assert len(msgs) == 1 and (await reg.load(s))["4h@vol_breakout"].status == "paused"
        # trades before live_from are not forward evidence
        await reg.sync(s, [("8h", "donchian", {"n": 100})])
        await reg.stamp_live_from(s, "8h@donchian", T0 + timedelta(days=30))
        old = [_trade(-1, i, ("swing_donchian", "8h")) for i in range(20)]
        assert await reg.check_alarms(s, old) == []


async def _seed(status=None, live_from=None):
    async def seed(sm):
        async with sm() as s:
            s.add(StrategyRegistry(spec_key="4h@vol_breakout", digest="x", status=status or "incubating",
                                   live_from=live_from))
            await s.commit()
    return seed


@pytest.mark.asyncio
async def test_paused_spec_does_not_trade():
    rows, logs = await run([("SOLUSDT", "vol_breakout", +1)], cap=0, seed=await _seed("paused"))
    assert rows == [] and logs[0].skip_reason == "spec_paused"


@pytest.mark.asyncio
async def test_first_trade_starts_the_specs_forward_clock():
    rows, _ = await run([("SOLUSDT", "vol_breakout", +1)], cap=0, seed=await _seed())
    assert len(rows) == 1
    lf = run.registry["4h@vol_breakout"].live_from
    assert lf is not None and abs((datetime.now(UTC).replace(tzinfo=None) - lf).total_seconds()) < 120


@pytest.mark.asyncio
async def test_swing_api_shows_each_specs_forward_clock():
    import json
    from unittest.mock import AsyncMock, patch

    from scheduler import health
    sm = await _db()
    async with sm() as s:
        await reg.sync(s, [("4h", "donchian", {"n": 100}), ("8h", "ichimoku", {})])
        await reg.stamp_live_from(s, "4h@donchian", T0)
    trades = [_trade(2, 1), _trade(-1, 2)]
    for t in trades:
        t.net_pnl, t.exit_reason, t.symbol = 1.0, "target", "solusdt"
    snap = {"cycle": SimpleNamespace(wallet=1000.0), "rows": [], "trades": trades}
    with patch.object(health, "_paper_db_snapshot", AsyncMock(return_value=snap)), \
         patch("storage.database.AsyncSessionFactory", sm):
        d = json.loads((await health._api_swing(SimpleNamespace(), None)).text)
    by = {x["spec"]: x for x in d["registry"]}
    assert by["4h@donchian"]["forward_trades"] == 2 and by["4h@donchian"]["forward_total_r"] == pytest.approx(1.0)
    assert by["4h@donchian"]["alarm_at_r"] == 28.7 and by["8h@ichimoku"]["live_from"] is None


@pytest.mark.asyncio
async def test_spec_is_promoted_after_enough_good_forward_trades_only():
    sm = await _db()
    async with sm() as s:
        await reg.sync(s, [("4h", "donchian", {"n": 100})])
        await reg.stamp_live_from(s, "4h@donchian", T0)
        good = [_trade(2 if i % 2 else -1, i) for i in range(29)]          # mean +0.5R but only 29 trades
        assert await reg.check_promotions(s, good) == []
        good.append(_trade(2, 30))
        msgs = await reg.check_promotions(s, good)
        assert len(msgs) == 1 and (await reg.load(s))["4h@donchian"].status == "trusted"


def test_trusted_weak_spec_gets_full_risk_and_cards_explain_risk(monkeypatch):
    from analysis import swing_book as sb
    from config.settings import settings
    for k, v in dict(swing_risk_pct=0.03, swing_risk_min=0.01, swing_adaptive_risk=True,
                     swing_weak_specs="4h@ichimoku", swing_weak_spec_factor=0.5).items():
        monkeypatch.setattr(settings, k, v)
    assert sb.risk_for("swing_ichimoku", "4h", 0.0, 0) == pytest.approx(0.015)
    assert sb.risk_for("swing_ichimoku", "4h", 0.0, 0, trusted=True) == pytest.approx(0.03)
    assert sb.risk_reason("swing_donchian", "4h", 0.0, 0) == "full risk"
    why = sb.risk_reason("swing_ichimoku", "4h", 0.25, 3)
    assert "25% below its peak" in why and "3 losses" in why and "half risk" in why and "floor" in why


def test_weekly_report_reads_like_a_summary():
    from analysis.weekly_report import format_weekly_report
    row = SimpleNamespace(spec_key="4h@donchian", status="incubating", live_from=T0)
    trades = [_trade(2, 1), _trade(-1, 2)]
    txt = format_weekly_report({"4h@donchian": row}, trades, equity=5400, start=5000, withdrawn=0,
                               week_trades=trades, skips={"below_min_notional": 3, "regime_wild": 1})
    assert "Equity ₹5,400" in txt and "2 closed, 1 won, +1.0R" in txt
    assert "4h@donchian · incubating · 2 · +1.0R (+0.50)" in txt and "28.7R" in txt
    assert "below_min_notional 3" in txt


@pytest.mark.asyncio
async def test_weekly_report_job_sends_one_message():
    from unittest.mock import AsyncMock, patch

    from scheduler.runner import AppRunner
    from storage.repository import Repository
    sm = await _db()
    async with sm() as s:
        await Repository(s).start_cycle(starting_wallet=5000, target_wallet=1e7, leverage=10, stop_pct_of_margin=0.2,
                                        reward_risk=2, min_confidence=0.7, trailing_enabled=False, scaled_sizing=False,
                                        scaled_leverage=False, ladder_enabled=False, ladder_tight=False,
                                        sizing_floor_pct=0.25, sizing_ceiling_pct=0.25)
        await reg.sync(s, [("4h", "donchian", {"n": 100})])
    runner = AppRunner()
    runner.notifier = AsyncMock()
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        await runner._weekly_forward_report_job()
    txt = runner.notifier.send_text.call_args[0][0]
    assert "Weekly swing report" in txt and "4h@donchian" in txt and "Equity ₹5,000" in txt


def test_shortfall_is_positive_when_the_entry_is_worse():
    from analysis import swing_book as sb
    assert sb.shortfall(+1, 100.5, 100.0, 0, 15 * 60_000) == (pytest.approx(50.0), 15.0)   # long paid 0.5% more
    assert sb.shortfall(-1, 100.5, 100.0, 0, 0)[0] == pytest.approx(-50.0)                # short sold higher: better


@pytest.mark.asyncio
async def test_opened_swing_trade_card_records_the_shortfall():
    from sqlalchemy import select

    from storage.models import TradeEvent
    sm_holder = {}

    async def seed(sm):
        sm_holder["sm"] = sm
    rows, _ = await run([("SOLUSDT", "vol_breakout", +1)], cap=0, seed=seed)
    assert len(rows) == 1
    async with sm_holder["sm"]() as s:
        notes = (await s.execute(select(TradeEvent.note).where(TradeEvent.kind == "opened"))).scalars().all()
    assert any("shortfall=" in n and "late=" in n for n in notes)
