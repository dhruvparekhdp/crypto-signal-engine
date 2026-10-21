"""Closed-trade R is measured against the ORIGINAL stop (21 Oct 2026: the swing page showed SOL +0.99R and BNB +3.09R;
the ladder had moved their stops near the entry, and R was divided by that moved stop - the trade notes said +0.23R and
+0.76R, which is right)."""
from types import SimpleNamespace

import pytest

from analysis.strategy_registry import trade_r


def _trade(**kw):
    base = dict(side="short", entry_price=100.0, exit_price=99.0, stop_price=99.5, initial_stop_price=105.0,
                trading_fees=0.0, funding_paid=0.0, margin=10.0, leverage=10.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_r_uses_the_original_stop_not_the_ladder_moved_one():
    assert trade_r(_trade()) == pytest.approx(1.0 / 5.0)          # 1 point won on 5 points of original risk


def test_old_rows_without_an_original_stop_fall_back_to_the_stop():
    assert trade_r(_trade(initial_stop_price=0.0, stop_price=105.0)) == pytest.approx(0.2)
    assert trade_r(SimpleNamespace(side="long", entry_price=100.0, exit_price=110.0, stop_price=95.0, trading_fees=0.0,
                                   funding_paid=0.0, margin=10.0, leverage=10.0)) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_a_closed_trade_keeps_its_original_stop():
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from storage.models import Base, PaperTrade
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    pos = SimpleNamespace(symbol="solusdt", side=SimpleNamespace(value="short"), trade_mode="swing", signal_price=107.6,
                          entry_price=107.6, coin_qty=1.0, margin=100.0, leverage=6.0, usdt_inr=85.0, stop_price=107.0,
                          initial_stop_price=114.1, target_price=88.0, partial_pnl=0.0, signal_type="swing_ichimoku",
                          timeframe="4h", confidence=0.8, opened_at=now)
    trade = SimpleNamespace(position=pos, exit_price=106.0, reason=SimpleNamespace(value="expiry"), gross_pnl=1.0,
                            fees_paid=0.1, funding_paid=0.0, net_pnl=0.9, return_on_margin=0.9, wallet_after=1000.0,
                            entry_slippage_pct=0.0, hours_held=168.0, closed_at=now)
    async with sm() as s:
        await Repository(s).record_trade(1, trade)
        row = (await s.execute(select(PaperTrade))).scalars().one()
    assert row.initial_stop_price == 114.1 and row.stop_price == 107.0


@pytest.mark.asyncio
async def test_backfill_recovers_the_original_stop_from_the_signal(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scripts.backfill_initial_stops import backfill
    from storage.models import Base, CryptoSignalLog, PaperTrade
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    opened = datetime(2026, 10, 13, 20, 3)
    async with sm() as s:
        await Repository(s).log_crypto_signal(symbol="solusdt", signal_type="swing_ichimoku", direction="short",
                                              trigger_description="x", confidence=0.8, current_price=107.5,
                                              target_price=88.0, stop_loss=114.0, edge_pct=0, stake_pct=0,
                                              timeframe="4h", trade_mode="swing")
        await s.execute(update(CryptoSignalLog).values(timestamp=opened - timedelta(minutes=1)))
        s.add(PaperTrade(cycle_id=1, symbol="solusdt", side="short", trade_mode="swing", signal_price=107.6,
                         entry_price=107.62, exit_price=106.0, coin_qty=1.0, margin=100.0, leverage=6.0, usdt_inr=85.0,
                         stop_price=109.25, initial_stop_price=0.0, target_price=88.0, exit_reason="expiry",
                         gross_pnl=1.0, trading_fees=0.1, funding_paid=0.0, net_pnl=0.9, wallet_after=1000.0, signal_type="swing_ichimoku",
                         timeframe="4h", opened_at=opened, closed_at=opened + timedelta(days=7)))
        await s.commit()
    monkeypatch.setattr("storage.database.AsyncSessionFactory", sm)
    rows = await backfill(backup_dir=tmp_path)
    assert rows[0]["original_stop"] == pytest.approx(107.6 + 6.5)       # signal price + the signal's 6.5 stop distance
    assert rows[0]["r_after"] < rows[0]["r_before"]                     # the moved stop had inflated it
    async with sm() as s:
        assert (await s.execute(select(PaperTrade))).scalars().one().initial_stop_price == pytest.approx(114.1)
    assert list(tmp_path.glob("paper_trades_*.jsonl.gz"))
