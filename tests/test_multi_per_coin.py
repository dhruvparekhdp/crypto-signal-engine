"""Several swing trades on one coin (owner 28 Oct 2026: "switch it on with one rule it stops after 3 days, then we
will review"): up to 3 per coin, same direction, different strategies; Delta nets them into one position, so each
trade keeps its own record, stop and target, and closing one never touches the others."""
from datetime import UTC, datetime

import pytest

from analysis import swing_book as sb
from execution.delta_india import DeltaError, Product
from tests.test_delta_ladder import FakeDelta, _book

SHORT = ("xrpusdt", "short", 1.40, 1.47, 1.19)


class NetDelta(FakeDelta):
    """Delta's netting: one position per coin; a reduce-only order takes off only its own size."""

    def __init__(self):
        super().__init__()
        self.lev = []

    async def place_market(self, product_id, side, size, reduce_only=False):
        sign = 1 if side == "buy" else -1
        self.size += sign * size
        return self._order("market", side=side, size=size, reduce_only=reduce_only, average_fill_price="1.4")

    async def set_leverage(self, product_id, lev):
        self.lev.append(lev)
        return {}

    async def open_orders(self, product_id):
        cancelled = {kw["id"] for k, kw in self.calls if k == "cancel"}
        return [{"id": self.next_id_of(k, kw), "stop_order_type": "stop_loss_order" if k == "stop" else "take_profit_order"}
                for k, kw in self.calls if k in ("stop", "tp") and self.next_id_of(k, kw) not in cancelled
                and self.next_id_of(k, kw) not in self.filled]

    filled: set = set()
    _ids: dict = {}

    def _order(self, kind, **kw):
        o = super()._order(kind, **kw)
        self._ids[id(self.calls[-1][1])] = o["id"]
        return o

    def next_id_of(self, kind, kw):
        return self._ids.get(id(kw))


def test_the_limit_is_3_until_the_review_date_then_1_by_itself(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "swing_max_per_coin", 3)
    monkeypatch.setattr(settings, "swing_multi_per_coin_until", "2026-10-31T12:00:00")
    assert sb.per_coin_limit(datetime(2026, 10, 31, 11, 59, tzinfo=UTC)) == 3
    assert sb.per_coin_limit(datetime(2026, 10, 31, 12, 0, tzinfo=UTC)) == 1
    monkeypatch.setattr(settings, "swing_multi_per_coin_until", "not a date")
    assert sb.per_coin_limit(datetime(2026, 10, 29, tzinfo=UTC)) == 1           # a bad date never leaves it on


def test_same_direction_and_a_different_strategy_only():
    open_ = [(-1, "swing_donchian@4h")]
    assert sb.coin_refusal([], 1, "swing_donchian@4h", 1) == ""
    assert sb.coin_refusal(open_, -1, "swing_keltner_break@4h", 3) == ""
    assert sb.coin_refusal(open_, -1, "swing_donchian@4h", 3) == "same_strategy_open_in_symbol"
    assert sb.coin_refusal(open_, 1, "swing_keltner_break@4h", 3) == "opposite_side_open_in_symbol"
    assert sb.coin_refusal(open_, -1, "swing_keltner_break@4h", 1) == "already_open_in_symbol"
    three = open_ + [(-1, "a"), (-1, "b")]
    assert sb.coin_refusal(three, -1, "c", 3) == "already_open_in_symbol"


@pytest.mark.asyncio
async def test_each_trade_on_a_coin_has_its_own_record_stop_and_target(tmp_path):
    d = NetDelta()
    book = _book(tmp_path, d)
    book.max_open = 4
    p1 = await book.on_paper_open(*SHORT, "swing_donchian", "swing_donchian@4h", 3)
    p2 = await book.on_paper_open(*SHORT, "swing_keltner_break", "swing_keltner_break@8h", 3)
    assert p1.ok and p2.ok
    recs = book._load_state()["positions"]
    assert sorted(recs) == ["XRPUSD", "XRPUSD#2"]
    assert recs["XRPUSD#2"]["spec"] == "swing_keltner_break@8h" and recs["XRPUSD#2"]["delta_symbol"] == "XRPUSD"
    stops = [kw for k, kw in d.calls if k == "stop"]
    assert [s["size"] for s in stops] == [p1.size, p2.size]                    # each stop covers its own trade only
    assert d.size == -(p1.size + p2.size)
    assert d.lev[1] <= d.lev[0]                                               # leverage never raised on the coin
    same = await book.on_paper_open(*SHORT, "swing_donchian", "swing_donchian@4h", 3)
    assert not same.ok and same.reason == "same_strategy_open_on_delta"
    opp = await book.on_paper_open("xrpusdt", "long", 1.40, 1.33, 1.61, "swing_ichimoku", "swing_ichimoku@4h", 3)
    assert not opp.ok and opp.reason == "opposite_side_open_on_delta"
    after = await book.on_paper_open(*SHORT, "swing_ichimoku", "swing_ichimoku@4h", 1)    # the 3 days are over
    assert not after.ok and after.reason == "already_open_on_delta"


@pytest.mark.asyncio
async def test_closing_one_trade_sells_only_its_contracts_and_cancels_only_its_orders(tmp_path):
    d = NetDelta()
    book = _book(tmp_path, d)
    book.max_open = 4
    p1 = await book.on_paper_open(*SHORT, "swing_donchian", "swing_donchian@4h", 3)
    p2 = await book.on_paper_open(*SHORT, "swing_keltner_break", "swing_keltner_break@8h", 3)
    rec2 = book._load_state()["positions"]["XRPUSD#2"]
    d.calls.clear()
    out = await book.on_paper_close("xrpusdt", "target", "swing_keltner_break@8h")
    assert out["ok"]
    assert ("market", {"side": "buy", "size": p2.size, "reduce_only": True, "average_fill_price": "1.4"}) in d.calls
    assert {kw["id"] for k, kw in d.calls if k == "cancel"} == {rec2["stop_order_id"], rec2["tp_order_id"]}
    assert not any(k == "cancel_all" for k, _ in d.calls)
    assert list(book._load_state()["positions"]) == ["XRPUSD"] and d.size == -p1.size
    out = await book.on_paper_close("xrpusdt", "stop", "swing_donchian@4h")       # the last one: flatten as before
    assert out["ok"] and d.size == 0 and any(k == "cancel_all" for k, _ in d.calls)


@pytest.mark.asyncio
async def test_a_trade_opened_before_strategies_were_stored_still_closes(tmp_path):
    d = NetDelta()
    book = _book(tmp_path, d)
    await book.on_paper_open(*SHORT, "swing_donchian")                           # the old call: no strategy
    assert (await book.on_paper_close("xrpusdt", "stop", "swing_donchian@4h"))["ok"] and d.size == 0


@pytest.mark.asyncio
async def test_reconcile_drops_only_the_trade_whose_stop_filled_and_restores_a_missing_stop(tmp_path):
    d = NetDelta()
    book = _book(tmp_path, d)
    book.max_open = 4
    book.refresh_capital = lambda: _none()
    book._loss_guard = lambda: _empty()
    p1 = await book.on_paper_open(*SHORT, "swing_donchian", "swing_donchian@4h", 3)
    await book.on_paper_open(*SHORT, "swing_keltner_break", "swing_keltner_break@8h", 3)
    recs = book._load_state()["positions"]
    d.filled = {recs["XRPUSD#2"]["stop_order_id"]}                             # trade 2's stop fired on Delta
    d.size = -p1.size
    events = await book.reconcile()
    assert [e["event"] for e in events if e.get("key")] == ["closed_on_exchange"]
    assert list(book._load_state()["positions"]) == ["XRPUSD"]
    assert ("cancel", {"id": recs["XRPUSD#2"]["tp_order_id"]}) in d.calls       # its target order is cleaned up


async def _none():
    return None


async def _empty():
    return ""


@pytest.mark.asyncio
async def test_paper_opens_a_second_trade_on_a_coin_only_by_the_rule(monkeypatch):
    from config.settings import settings
    from tests.test_phase1 import _swing
    monkeypatch.setattr(settings, "swing_max_per_coin", 3)
    monkeypatch.setattr(settings, "swing_multi_per_coin_until", "2099-01-01T00:00:00")
    monkeypatch.setattr(settings, "paper_mirrors_live", False)
    sigs = [("BTCUSDT", "donchian", 1, 60000.0), ("BTCUSDT", "keltner_break", 1, 60000.0),
            ("BTCUSDT", "donchian", 1, 60000.0), ("BTCUSDT", "ichimoku", -1, 60000.0)]
    rows, logs, _ = await _swing(sigs)
    assert sorted(r.signal_type for r in rows) == ["swing_donchian", "swing_keltner_break"]
    skipped = sorted(x.skip_reason for x in logs if x.skip_reason)
    assert skipped == ["opposite_side_open_in_symbol", "same_strategy_open_in_symbol"]
