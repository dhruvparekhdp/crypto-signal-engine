"""Live swing book (not wired into the engine yet): sizing, signing and the monitor's safety rules, on a fake exchange."""
from types import SimpleNamespace

import pytest

from execution.binance_futures import BinanceFutures, SymbolRules, parse_rules
from execution.live_book import LiveBook, LivePosition, LiveState, plan_entry

SOL = SymbolRules("SOLUSDT", step=0.01, min_qty=0.01, tick=0.01, min_notional=5.0)
BTC = SymbolRules("BTCUSDT", step=0.001, min_qty=0.001, tick=0.1, min_notional=100.0)


class TestSizing:
    def test_a_20_dollar_account_can_trade_sol_within_the_risk_limit(self):
        p = plan_entry(20, 20, 20, 0.015, 0.025, 150.0, 144.0, SOL, 10)   # 4% stop
        assert p.ok and p.notional >= 5.0
        assert p.risk_usdt <= 20 * 0.025 + 1e-9
        assert p.leverage == 1                                        # plenty of margin: no leverage needed

    def test_btc_is_refused_because_its_minimum_order_risks_too_much(self):
        p = plan_entry(20, 20, 20, 0.015, 0.025, 60000.0, 57000.0, BTC, 10)
        assert not p.ok and p.reason.startswith("min_order_risks_")

    def test_leverage_rises_only_as_free_margin_shrinks_and_stops_at_the_ceiling(self):
        p = plan_entry(20, 3, 20, 0.015, 0.025, 150.0, 144.0, SOL, 10)
        assert p.ok and 1 < p.leverage <= 10
        p = plan_entry(20, 0.5, 20, 0.015, 0.025, 150.0, 144.0, SOL, 10)
        assert not p.ok and p.reason.startswith("needs_")

    def test_the_cap_limits_a_bigger_account(self):
        p = plan_entry(1000, 1000, 20, 0.015, 0.025, 150.0, 144.0, SOL, 10)
        assert p.risk_usdt <= 20 * 0.025 + 1e-9

    def test_quantity_respects_the_lot_step(self):
        p = plan_entry(20, 20, 20, 0.015, 0.025, 150.0, 144.0, SOL, 10)
        assert abs(p.qty / SOL.step - round(p.qty / SOL.step)) < 1e-9


def test_signature_matches_binance_documentation_example():
    c = BinanceFutures("key", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    q = c._sign({"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1, "price": 0.1,
                 "recvWindow": 5000, "timestamp": 1499827319559})
    assert q.endswith("signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")


def test_rules_are_read_from_exchange_info():
    info = {"symbols": [{"symbol": "SOLUSDT", "contractType": "PERPETUAL", "status": "TRADING", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.0100"}, {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    r = parse_rules(info)["SOLUSDT"]
    assert (r.step, r.tick, r.min_notional) == (0.01, 0.01, 5.0)
    assert r.floor_qty(0.0379) == 0.03 and r.round_price(144.004) == 144.0


class FakeExchange:
    def __init__(self):
        self.amt, self.orders, self.calls = 0.0, [], []
        self.fail_protect = False

    async def rules(self, s): return SOL
    async def usdt_balance(self): return {"balance": 20.0, "available": 20.0}
    async def position(self, s): return {"amt": self.amt, "entry": 150.0, "mark": 150.0, "unrealized": 0.0}
    async def prepare(self, s, lev): self.calls.append(("prepare", lev))

    async def market(self, s, side, qty, reduce_only=False, client_id=None):
        self.calls.append(("market", side, qty, reduce_only))
        self.amt = 0.0 if reduce_only else (qty if side == "BUY" else -qty)
        return {"avg_price": 150.3, "qty": qty, "order_id": 1, "status": "FILLED"}

    async def protect(self, s, close_side, kind, trigger):
        if self.fail_protect:
            from execution.binance_futures import BinanceError
            raise BinanceError(400, -2021, "would immediately trigger")
        self.orders.append((kind, trigger))
        return {"id": len(self.orders), "via": "order"}

    async def cancel_all(self, s): self.orders.clear()
    async def open_protection(self, s): return len(self.orders)
    async def realized_since(self, s, t): return {"realized": 0.9, "fees": -0.02, "funding": 0.0, "net": 0.88}


def cfg(**kw):
    base = dict(live_max_open=0, live_max_balance_usdt=20, live_daily_loss_pct=0.06, live_risk_pct=0.015,
                live_max_risk_pct=0.025, live_max_leverage=10, swing_hold_minutes=10080)
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def book(tmp_path):
    ex = FakeExchange()
    return LiveBook(ex, LiveState(str(tmp_path / "s.json"), str(tmp_path / "t.jsonl")), cfg()), ex


@pytest.mark.asyncio
async def test_entry_places_stop_and_target_from_the_actual_fill(book):
    b, ex = book
    lp = await b.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 1_000_000)
    assert lp is not None and lp.entry == 150.3
    assert ex.orders == [("STOP_MARKET", 144.3), ("TAKE_PROFIT_MARKET", 168.3)]
    assert LiveState(str(b.state.path), str(b.state.trades_path)).positions["SOLUSDT"].qty == lp.qty   # persisted


@pytest.mark.asyncio
async def test_a_position_that_cannot_be_protected_is_closed_at_once(book):
    b, ex = book
    ex.fail_protect = True
    assert await b.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 1_000_000) is None
    assert ex.amt == 0.0 and ("market", "SELL", ex.calls[1][2], True) in ex.calls
    assert b.state.trades()[-1]["reason"] == "protection_failed"


@pytest.mark.asyncio
async def test_monitor_records_a_trade_the_exchange_closed(book):
    b, ex = book
    await b.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 1_000_000)
    ex.amt = 0.0                                                     # target hit on the exchange
    await b.monitor(2_000_000)
    t = b.state.trades()[-1]
    assert t["reason"] == "target" and t["net"] == pytest.approx(0.88) and b.state.positions == {}
    assert t["entry_slippage_pct"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_monitor_rearms_missing_protection_and_closes_at_the_time_limit(book):
    b, ex = book
    lp = await b.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 1_000_000)
    ex.orders.clear()
    await b.monitor(2_000_000)
    assert len(ex.orders) == 2
    await b.monitor(lp.expires_ms + 1)
    assert ex.amt == 0.0 and b.state.trades()[-1]["reason"] == "time_limit"


@pytest.mark.asyncio
async def test_kill_switch_and_daily_loss_block_new_entries(book, tmp_path):
    b, ex = book
    b.state.killed = True
    assert await b.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 1_000_000) is None
    b.state.killed = False
    b.state.record_trade({"closed_ms": 86_400_000 * 10 + 5, "net": -1.5})
    assert await b.open_from_paper("solusdt", "long", 150.0, 6.0, "swing_donchian", 86_400_000 * 10 + 60) is None
    assert b.state.skips[-1]["reason"] == "daily_loss_limit"
