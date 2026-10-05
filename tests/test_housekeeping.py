"""Fixes from the 5 Oct review: strict JSON in the dashboard API, linked pre-trade reviews, no AI on shadow signals."""
import json
import math
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.models import Base


def test_dashboard_api_never_emits_nan_or_infinity():
    from scheduler import health
    text = health.json.dumps({"target": math.inf, "rows": [{"pf": float("nan")}, {"x": 1.25}]})
    assert json.loads(text) == {"target": None, "rows": [{"pf": None}, {"x": 1.25}]}
    assert "Infinity" not in health._json_response({"t": -math.inf}).text


def test_dashboard_module_routes_every_json_response_through_the_strict_encoder():
    from pathlib import Path
    src = Path("scheduler/health.py").read_text()
    assert "web.json_response(" not in src.replace("return web.json_response(data, **kw)", "")


@pytest.mark.asyncio
async def test_a_pre_trade_review_is_linked_to_its_signal_once_the_signal_is_logged():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    from storage.repository import Repository
    async with sm() as s:
        await Repository(s).save_review("pre", "btcusdt", signal_type="confluence", verdict="APPROVE")
        await Repository(s).save_review("pre", "ethusdt", signal_type="confluence", verdict="REJECT")
    async with sm() as s:
        n = await Repository(s).link_pre_reviews("btcusdt", "confluence", 42)
    assert n == 1
    from sqlalchemy import select
    from storage.models import SignalReview
    async with sm() as s:
        rows = {r.symbol: r.signal_log_id for r in (await s.execute(select(SignalReview))).scalars()}
    assert rows == {"btcusdt": 42, "ethusdt": 0}


@pytest.mark.asyncio
async def test_shadow_only_signals_do_not_spend_ai_calls():
    from scheduler.runner import AppRunner
    runner = AppRunner()
    runner.groq_sentinel = SimpleNamespace(is_available=True, review_signal_candidate=AsyncMock(side_effect=AssertionError("no AI call")))
    sig = SimpleNamespace(symbol="btcusdt", signal_type="confluence")
    with patch("scheduler.runner.settings.groq_signal_review_enabled", True), \
         patch("scheduler.runner.settings.paper_open_families", "swing"):
        assert await runner._ai_review_candidate(sig, None, {}, None) == ("", "")


def test_briefing_waits_after_a_quiet_one_but_not_near_an_event():
    from datetime import timedelta
    from scheduler.runner import _briefing_can_wait
    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    quiet = SimpleNamespace(events="[]", created_at=now - timedelta(minutes=40))
    busy = SimpleNamespace(events='[{"headline": "Fed cuts"}]', created_at=now - timedelta(minutes=40))
    old_quiet = SimpleNamespace(events="[]", created_at=now - timedelta(minutes=200))
    assert _briefing_can_wait(quiet, now, event_near=False) is True
    assert _briefing_can_wait(quiet, now, event_near=True) is False
    assert _briefing_can_wait(busy, now, event_near=False) is False
    assert _briefing_can_wait(old_quiet, now, event_near=False) is False
    assert _briefing_can_wait(None, now, event_near=False) is False


@pytest.mark.asyncio
async def test_swing_endpoint_reports_trades_in_r_against_the_backtest():
    from scheduler import health
    win = SimpleNamespace(signal_type="swing_keltner_break", side="long", entry_price=100.0, stop_price=94.0, exit_price=118.0,
                          trading_fees=0.0, funding_paid=0.0, margin=100.0, leverage=1.0, net_pnl=18.0, symbol="btcusdt",
                          exit_reason="target", closed_at=datetime(2026, 10, 5, tzinfo=UTC))
    loss = SimpleNamespace(**{**win.__dict__, "exit_price": 94.0, "net_pnl": -6.0, "exit_reason": "stop"})
    old = SimpleNamespace(**{**win.__dict__, "signal_type": "confluence"})
    snap = {"cycle": SimpleNamespace(wallet=1700.0), "rows": [], "trades": [win, loss, old], "running": []}
    runner = SimpleNamespace(_swing_last={"BTCUSDT": {"signal": None}})
    with patch.object(health, "_paper_db_snapshot", AsyncMock(return_value=snap)):
        resp = await health._api_swing(runner, None)
    d = json.loads(resp.text)
    assert d["closed"]["n"] == 2                       # the intraday trade is not part of the swing book
    assert d["closed"]["by_strategy"]["keltner_break"]["n"] == 2
    assert abs(d["closed"]["avg_r"] - (3.0 - 1.0) / 2) < 1e-9
    assert d["rules"]["target"] == "3R" and d["scan"] == {"BTCUSDT": {"signal": None}}


@pytest.mark.asyncio
async def test_swing_endpoint_works_with_no_running_cycle():
    from scheduler import health
    with patch.object(health, "_paper_db_snapshot", AsyncMock(return_value={"cycle": None, "recent": []})):
        resp = await health._api_swing(SimpleNamespace(), None)
    assert json.loads(resp.text)["closed"]["n"] == 0
