"""Trade notes (owner, 18 Oct 2026, AI plan step 7): from facts only, after the order, never in its way."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from analysis import trade_notes as tn


def _pos():
    return SimpleNamespace(symbol="xrpusdt", side=SimpleNamespace(value="short"), entry_price=1.30, stop_price=1.365,
                           target_price=1.105, leverage=8.0, opened_at=datetime(2026, 10, 18, 8, tzinfo=UTC))


def test_open_facts_carry_only_what_we_know():
    f = tn.open_facts(_pos(), "swing_keltner_break", "4h", 0.03, SimpleNamespace(btc_vol_rank=0.41), {"grade": "B"},
                      live=True)
    assert (f["kind"], f["coin"], f["side"], f["strategy"], f["timeframe"]) == ("open", "XRPUSDT", "short",
                                                                                 "keltner break", "4h")
    assert (f["stop_distance_pct"], f["leverage"], f["risk_pct_of_wallet"], f["also_on_live_delta"]) == (5.0, 8.0, 3.0,
                                                                                                         True)
    assert f["btc_volatility_rank"] == 0.41 and f["backtest_grade"] == "B" and f["target_in_R"] == 3


def test_close_facts_compute_r_and_list_the_news_while_open():
    row = SimpleNamespace(symbol="xrpusdt", side="short", entry_price=1.30, initial_stop_price=1.365, stop_price=1.30,
                          signal_type="swing_donchian", opened_at=datetime(2026, 10, 18, 8))
    trade = SimpleNamespace(exit_price=1.235, reason=SimpleNamespace(value="lock"), net_pnl=29.5)
    news = [(datetime(2026, 10, 18, 9, 30), "coindesk", "SEC delays XRP ETF decision")]
    f = tn.close_facts(row, trade, news, datetime(2026, 10, 19, 20, tzinfo=UTC))
    assert (f["result_R"], f["net_rupees"], f["how_it_ended"], f["ran_for_hours"]) == (1.0, 29.5, "lock", 36.0)
    assert f["news_while_open"] == ["18 Oct 09:30 UTC coindesk: SEC delays XRP ETF decision"]


def test_a_note_is_trimmed_and_junk_is_no_note():
    assert tn.clean_note({"note": "  Short   XRP on a Keltner break.  "}) == "Short XRP on a Keltner break."
    assert tn.clean_note({"note": 42}) == "" and tn.clean_note({}) == "" and tn.clean_note(None) == ""
    long = tn.clean_note({"note": "word " * 200})
    assert len(long) <= tn.MAX_CHARS and long.endswith("…")


def test_write_note_never_raises():
    with patch("collectors.llm_client.ask_json", new=AsyncMock(side_effect=RuntimeError("all down"))):
        assert asyncio.run(tn.write_note({"kind": "open"})) == ("", "")
    reply = SimpleNamespace(data={"note": "Opened short."}, served_by="gemini/flash-lite", __bool__=lambda s: True)
    with patch("collectors.llm_client.ask_json", new=AsyncMock(return_value=reply)):
        assert asyncio.run(tn.write_note({"kind": "open"})) == ("Opened short.", "gemini/flash-lite")


@pytest.mark.asyncio
async def test_the_note_goes_on_the_card_and_to_telegram_and_a_failure_does_neither():
    from scheduler.runner import AppRunner
    r = AppRunner.__new__(AppRunner)
    r.notifier = SimpleNamespace(send_text=AsyncMock())
    saved = []

    class Repo:
        def __init__(self, s):
            pass

        async def add_trade_events(self, ev):
            saved.extend(ev)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    with patch("scheduler.runner.Repository", Repo), patch("scheduler.runner.AsyncSessionFactory", Session), \
         patch("analysis.trade_notes.write_note", new=AsyncMock(return_value=("Short XRP <on> a break.", "gemini/x"))):
        assert await r._post_trade_note(_pos(), 2, {"kind": "open"}, True) == "Short XRP <on> a break."
    assert saved[0]["kind"] == "note" and saved[0]["field"] == "open" and "gemini/x" in saved[0]["note"]
    text = r.notifier.send_text.await_args.args[0]
    assert "Trade note" in text and "XRPUSDT" in text and "&lt;on&gt;" in text        # model text is escaped
    saved.clear()
    r.notifier.send_text.reset_mock()
    with patch("scheduler.runner.Repository", Repo), patch("scheduler.runner.AsyncSessionFactory", Session), \
         patch("analysis.trade_notes.write_note", new=AsyncMock(return_value=("", ""))):
        assert await r._post_trade_note(_pos(), 2, {"kind": "open"}, True) == ""
    assert saved == [] and not r.notifier.send_text.await_count


@pytest.mark.asyncio
async def test_a_paper_open_schedules_the_note_after_the_order(monkeypatch):
    """The note is a separate task: the open (and the live order inside it) never waits for the AI."""
    from tests.test_paper_mirrors_live import run
    order = []

    async def slow_note(self, pos, cycle_id, facts, telegram):
        order.append(("note", facts["kind"], facts["coin"], facts["also_on_live_delta"]))
        return ""

    async def mirror(*a, **k):
        order.append(("live_order",))
        return True
    with patch("scheduler.runner.AppRunner._post_trade_note", slow_note):
        from config.settings import settings
        monkeypatch.setattr(settings, "trade_notes_enabled", True)
        rows, _, runner = await run([("XRPUSDT", "donchian", +1)], mirror=mirror)
        await asyncio.sleep(0)
    assert [r.symbol for r in rows] == ["xrpusdt"]
    assert order[0] == ("live_order",) and order[1] == ("note", "open", "XRPUSDT", True)


@pytest.mark.asyncio
async def test_the_swing_book_shows_each_trades_notes():
    from datetime import datetime as _dt

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scheduler import health
    from storage.models import Base, TradeEvent
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    opened = _dt(2026, 10, 18, 8, 0, 5)
    async with sm() as s:
        s.add_all([TradeEvent(cycle_id=2, symbol="xrpusdt", opened_at=opened, at=opened, kind="note", field="open",
                              old="", new="", note="Short XRP on a Keltner break."),
                   TradeEvent(cycle_id=2, symbol="xrpusdt", opened_at=opened, at=opened, kind="note", field="close",
                              old="", new="", note="Closed at the 1R lock, +Rs29.")])
        await s.commit()
    with patch("storage.database.AsyncSessionFactory", sm):
        notes = await health._swing_notes(2)
    k = health._note_key("XRPUSDT", opened.replace(tzinfo=UTC))
    assert notes[k] == {"open": "Short XRP on a Keltner break.", "close": "Closed at the 1R lock, +Rs29."}


@pytest.mark.asyncio
async def test_the_model_name_is_kept_in_the_record_but_not_shown():
    from datetime import datetime as _dt

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scheduler import health
    from storage.models import Base, TradeEvent
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    opened = _dt(2026, 10, 18, 8)
    async with sm() as s:
        s.add(TradeEvent(cycle_id=3, symbol="adausdt", opened_at=opened, at=opened, kind="note", field="open", old="",
                         new="", note="Long ADA on a Donchian break (100 bars). (gemini/gemini-3.5-flash-lite)"))
        await s.commit()
    with patch("storage.database.AsyncSessionFactory", sm):
        notes = await health._swing_notes(3)
    assert notes[health._note_key("adausdt", opened)]["open"] == "Long ADA on a Donchian break (100 bars)."
