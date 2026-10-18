"""Closing a paper trade on the owner's request (18 Oct 2026: an XRP trade opened before live started kept every new XRP
signal - and so the live book - out for up to 7 days). The request goes through the normal close path."""
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


async def _open_one(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis import swing_book as sb
    from analysis.crypto_state import CryptoState
    from analysis.regime_gate import Verdict
    from config.settings import settings
    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    runner._mirror_delta_india = AsyncMock(return_value=False)
    runner._delta_call = AsyncMock()
    runner._close_trade_note = AsyncMock(return_value="")
    monkeypatch.setattr(AppRunner, "_CLOSE_REQUESTS", str(tmp_path / "requests.json"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    for k, v in {"paper_trading_enabled": True, "session_filter_enabled": False, "paper_mirrors_live": True,
                 "delta_india_risk_pct": 0.03, "delta_india_usd_inr": 85.0, "trade_notes_enabled": True}.items():
        monkeypatch.setattr(settings, k, v)
    st = CryptoState(symbol="xrpusdt", base_asset="XRP")
    st.current_price, st.atr_14 = 0.6, 0.003
    runner.crypto_store._states["xrpusdt"] = st
    sig = sb.to_signal(sb.SwingSetup("XRPUSDT", "donchian", -1, 0, 0.009, 0.6), 0.6, datetime.now(UTC))
    sig.regime = Verdict(0.2, 15.0, [])
    runner._pending_swing.append(sig)
    return runner, sm, st


@pytest.mark.asyncio
async def test_a_requested_trade_closes_at_the_current_price_and_the_request_is_cleared(tmp_path, monkeypatch):
    from storage.repository import Repository
    runner, sm, st = await _open_one(tmp_path, monkeypatch)
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        await runner._paper_trading_job()                                # opens the XRP short
        async with sm() as s:
            cycle = await Repository(s).get_running_cycle()
            row = (await Repository(s).get_open_positions(cycle.id))[0]
        (tmp_path / "requests.json").write_text(json.dumps([
            {"symbol": "XRPUSDT", "opened_at": row.opened_at.isoformat()[:16], "why": "free XRP for live"}]))
        st.current_price = 0.59                                          # in profit for a short
        await runner._paper_trading_job()
        async with sm() as s:
            assert await Repository(s).get_open_positions(cycle.id) == []
            t = (await Repository(s).get_cycle_trades(cycle.id))[0]
    assert t.exit_reason == "manual" and t.exit_price == pytest.approx(0.59, rel=0.01) and t.net_pnl > 0
    runner._delta_call.assert_any_await("on_paper_close", "xrpusdt", "manual")       # Delta books follow
    runner._close_trade_note.assert_awaited()                                        # and the closing note
    assert json.loads((tmp_path / "requests.json").read_text()) == []
    done = [json.loads(x) for x in (tmp_path / "data" / "paper_close_requests_done.jsonl").read_text().splitlines()]
    assert done[0]["symbol"] == "XRPUSDT" and done[0]["net_pnl"] == pytest.approx(t.net_pnl, abs=0.01)


@pytest.mark.asyncio
async def test_a_request_for_another_trade_of_the_same_coin_closes_nothing(tmp_path, monkeypatch):
    from storage.repository import Repository
    runner, sm, st = await _open_one(tmp_path, monkeypatch)
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        await runner._paper_trading_job()
        (tmp_path / "requests.json").write_text(json.dumps([{"symbol": "xrpusdt", "opened_at": "2026-10-15T04:03"}]))
        await runner._paper_trading_job()
        async with sm() as s:
            cycle = await Repository(s).get_running_cycle()
            assert len(await Repository(s).get_open_positions(cycle.id)) == 1
