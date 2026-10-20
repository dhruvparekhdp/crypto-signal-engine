"""Spread / depth guard before a real-money entry (owner, 19 Oct 2026: "go ahead with ... spread guard")."""
import pytest

from execution.entry_guard import check
from tests.test_delta_ladder import FakeDelta, _book, _open_short


def book(bid=1.3999, ask=1.4001, size=1000):
    return {"buy": [{"price": str(bid), "size": size}, {"price": str(bid - 0.001), "size": size}],
            "sell": [{"price": str(ask), "size": size}, {"price": str(ask + 0.001), "size": size}]}


def test_a_normal_book_passes_and_reports_its_cost():
    g = check(book(), "sell", 5, 0.05)
    assert g.ok and g.spread_pct == pytest.approx(0.0143, abs=1e-3) and g.fill_price == pytest.approx(1.3999)


def test_wide_spread_thin_book_and_empty_book_are_refused():
    assert check(book(1.38, 1.42), "buy", 5, 0.05).reason == "spread_too_wide"          # 2.9% spread
    assert check(book(size=2), "buy", 5, 0.05).reason == "book_too_thin"                # 4 contracts visible
    assert check({"buy": [], "sell": [{"price": "1.4", "size": 9}]}, "buy", 1, 0.05).reason == "orderbook_empty"
    assert check(None, "buy", 1, 0.05).reason == "orderbook_empty"


def test_slippage_is_judged_against_the_stop():
    deep = {"buy": [{"price": "1.399", "size": 1}, {"price": "1.38", "size": 100}],
            "sell": [{"price": "1.401", "size": 1}, {"price": "1.42", "size": 100}]}
    assert check(deep, "buy", 10, 0.05).reason == "slippage_too_high"      # ~1.3% slip on a 5% stop = 0.26R
    assert check(deep, "buy", 10, 0.20).ok                                 # same slip on a 20% stop = 0.07R


def test_a_buy_walks_the_asks_and_a_sell_walks_the_bids():
    assert check(book(), "buy", 5, 0.05).fill_price == pytest.approx(1.4001)
    assert check(book(), "sell", 5, 0.05).fill_price == pytest.approx(1.3999)


class WideBook(FakeDelta):
    def __init__(self, raw):
        super().__init__()
        self.raw = raw

    async def orderbook(self, symbol):
        if self.raw is None:
            raise ValueError("no answer")
        return self.raw


@pytest.mark.asyncio
async def test_live_book_sends_no_order_into_a_wide_or_unreadable_book(tmp_path):
    for raw, why in ((book(1.36, 1.44), "spread_too_wide"), (None, "orderbook_unreadable")):
        fake = WideBook(raw)
        b = _book(tmp_path, fake, entry_guard=True)
        plan = await _open_short(b)
        assert not plan.ok and plan.reason == why and fake.calls == []      # no entry, no stop, no target
        assert "XRPUSD" not in (b._load_state().get("positions") or {})


@pytest.mark.asyncio
async def test_live_book_trades_normally_through_a_healthy_book(tmp_path):
    fake = WideBook(book())
    plan = await _open_short(_book(tmp_path, fake, entry_guard=True))
    assert plan.ok and [c[0] for c in fake.calls] == ["market", "stop", "tp"]


@pytest.mark.asyncio
async def test_paper_skips_what_live_would_skip(monkeypatch):
    from config.settings import settings
    from scheduler.runner import AppRunner
    r = AppRunner()

    async def wide(symbol):
        return book(1.36, 1.44)
    monkeypatch.setattr(r.delta_market, "fetch_orderbook", wide)
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_india_live_orders", True)
    monkeypatch.setattr(settings, "delta_india_kill_switch", False)
    assert await r._live_book_guard("xrpusdt", -1, 5, 0.05, {"xrpusdt"}) == "spread_too_wide"
    assert await r._live_book_guard("ethusdt", -1, 5, 0.05, {"xrpusdt"}) == ""          # not a live coin
    monkeypatch.setattr(settings, "delta_india_live_orders", False)
    assert await r._live_book_guard("xrpusdt", -1, 5, 0.05, {"xrpusdt"}) == ""          # no real money on
