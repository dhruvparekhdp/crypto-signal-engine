"""Delta India: the profit ladder on the exchange (13 Oct 2026). Fake exchange: no network, no real orders."""
import pytest

from execution.delta_book import DeltaBook
from execution.delta_india import DeltaError, Product, round_to_tick


class FakeDelta:
    def __init__(self, fail_edit=False):
        self.calls, self.fail_edit, self.size, self.next_id = [], fail_edit, 0, 100
        self.products = {"XRPUSD": Product(14969, "XRPUSD", 1.0, 0.0001, 1.0, "XRP")}

    async def product(self, sym):
        return self.products.get(sym)

    async def inr_available(self):
        return 500.0

    def _order(self, kind, **kw):
        self.next_id += 1
        self.calls.append((kind, kw))
        return {"id": self.next_id, **kw}

    async def place_market(self, product_id, side, size, reduce_only=False):
        if not reduce_only:
            self.size = size if side == "buy" else -size
        else:
            self.size = 0
        return self._order("market", side=side, size=size, reduce_only=reduce_only, average_fill_price="1.4")

    async def place_stop_market(self, product_id, side, size, stop_price):
        return self._order("stop", side=side, size=size, stop_price=stop_price)

    async def place_take_profit_market(self, product_id, side, size, stop_price):
        return self._order("tp", side=side, size=size, stop_price=stop_price)

    async def edit_stop(self, order_id, product_id, size, stop_price):
        if self.fail_edit:
            raise DeltaError(400, {"error": "edit_failed"})
        self.calls.append(("edit", {"id": order_id, "stop_price": stop_price}))
        return {"id": order_id, "stop_price": stop_price}

    async def cancel_order(self, order_id, product_id):
        self.calls.append(("cancel", {"id": order_id}))
        return {}

    async def cancel_all(self, product_id):
        self.calls.append(("cancel_all", {"product_id": product_id}))
        return {}

    async def position_size(self, product_id):
        return self.size

    async def open_orders(self, product_id):
        return [{"stop_order_type": "stop_loss_order"}, {"stop_order_type": "take_profit_order"}]


def _book(tmp_path, client, mode="live", live=True, **kw):
    b = DeltaBook(client, mode=mode, live_orders=live, risk_pct=0.03, max_open=3, usd_inr=86.0,
                  shadow_path=tmp_path / "shadow.jsonl", state_path=tmp_path / "state.json", **kw)
    b.retry_delay_s = 0
    return b


async def _open_short(book):
    # short XRP at 1.40, stop 1.47 (1R = 0.07), target 1.19
    return await book.on_paper_open("xrpusdt", "short", 1.40, 1.47, 1.19, "swing_keltner_break")


@pytest.mark.asyncio
async def test_live_open_places_entry_stop_and_target_and_keeps_order_ids(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    plan = await _open_short(book)
    assert plan.ok and [c[0] for c in fake.calls] == ["market", "stop", "tp"]
    pos = book._load_state()["positions"]["XRPUSD"]
    assert pos["stop_order_id"] == 102 and pos["tp_order_id"] == 103 and pos["exchange_stop"] == 1.47


@pytest.mark.asyncio
async def test_ladder_lock_tightens_the_exchange_backstop_half_r_behind(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    await _open_short(book)
    # engine locks +0.5R on the short: stop 1.365 -> backstop 1.365 + 0.035 = 1.40 (break-even on the exchange)
    res = await book.on_stop_change("xrpusdt", 1.365)
    assert res["reason"] == "amended" and res["exchange_stop"] == pytest.approx(1.40)
    assert fake.calls[-1] == ("edit", {"id": 102, "stop_price": pytest.approx(1.40)})
    # a looser engine stop never loosens the exchange stop
    assert (await book.on_stop_change("xrpusdt", 1.45))["reason"] == "unchanged"
    # break-even lock on the engine (1.40) -> backstop would be 1.435, looser than 1.40 already set: unchanged
    assert (await book.on_stop_change("xrpusdt", 1.40))["reason"] == "unchanged"


@pytest.mark.asyncio
async def test_backstop_is_never_looser_than_the_original_stop(tmp_path):
    book = _book(tmp_path, FakeDelta())
    assert book.backstop_for(-1, 1.47, 0.07, 1.46) == pytest.approx(1.47)   # 1.46 + 0.035 would be looser
    assert book.backstop_for(+1, 94.0, 6.0, 97.0) == pytest.approx(94.0)


@pytest.mark.asyncio
async def test_failed_edit_falls_back_to_cancel_and_replace(tmp_path):
    fake = FakeDelta(fail_edit=True)
    book = _book(tmp_path, fake)
    await _open_short(book)
    res = await book.on_stop_change("xrpusdt", 1.30)
    assert res["reason"] == "amended"
    kinds = [c[0] for c in fake.calls]
    assert kinds[-2:] == ["stop", "cancel"]                     # new stop FIRST: never zero stops
    assert book._load_state()["positions"]["XRPUSD"]["stop_order_id"] == fake.next_id


@pytest.mark.asyncio
async def test_engine_close_flattens_on_delta_and_cancels_leftovers(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    await _open_short(book)
    res = await book.on_paper_close("xrpusdt", "profit_lock")
    assert res["reason"] == "closed"
    assert ("market", {"side": "buy", "size": 13, "reduce_only": True, "average_fill_price": "1.4"}) in fake.calls \
        or any(c[0] == "market" and c[1]["reduce_only"] for c in fake.calls)
    assert fake.calls[-1][0] == "cancel_all" and "XRPUSD" not in book._load_state()["positions"]


@pytest.mark.asyncio
async def test_reconcile_notices_a_position_delta_closed_itself(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    await _open_short(book)
    fake.size = 0                                               # backstop or target filled on the exchange
    events = await book.reconcile()
    assert events[0]["event"] == "closed_on_exchange" and "XRPUSD" not in book._load_state()["positions"]


@pytest.mark.asyncio
async def test_kill_switch_refuses_new_orders(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake, kill_switch=True)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason == "kill_switch" and fake.calls == []


@pytest.mark.asyncio
async def test_shadow_mode_rehearses_the_ladder_without_orders(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake, mode="shadow", live=False)
    await _open_short(book)
    res = await book.on_stop_change("xrpusdt", 1.365)
    assert res["reason"] == "amended" and fake.calls == []      # logged as "would amend", nothing placed
    closed = await book.on_paper_close("xrpusdt", "profit_lock")
    assert closed["reason"] == "closed" and fake.calls == []


def test_stops_round_to_the_tick_without_loosening():
    assert round_to_tick(1.36543, 0.001, -1) == pytest.approx(1.365)   # short stop rounds down (tighter)
    assert round_to_tick(97.123, 0.05, +1) == pytest.approx(97.15)     # long stop rounds up (tighter)


@pytest.mark.asyncio
async def test_wallet_reads_inr_value_of_a_usd_wallet_and_the_exchange_rate():
    from execution.delta_india import DeltaIndia

    class Fake(DeltaIndia):
        def __init__(self):
            pass

        async def balances(self):
            return [{"asset_symbol": "USD", "balance": "100", "available_balance": "100", "available_balance_inr": "8500"},
                    {"asset_symbol": "INR", "balance": "0", "available_balance": "0", "available_balance_inr": "0"}]
    d = Fake()
    assert await d.wallet_inr() == (8500.0, 85.0)
    assert await d.inr_available() == 8500.0


@pytest.mark.asyncio
async def test_cancel_all_sends_the_product_in_the_body():
    from execution.delta_india import DeltaIndia
    sent = {}

    class Fake(DeltaIndia):
        def __init__(self):
            pass

        async def _request(self, method, path, *, params=None, body=None, signed=True):
            sent.update(method=method, path=path, params=params, body=body)
            return {}
    await Fake().cancel_all(93723)
    assert sent["method"] == "DELETE" and sent["params"] is None and sent["body"]["product_id"] == 93723


class FlakyEdit(FakeDelta):
    """First edit fails, the retry works (what the testnet did on 14 Oct)."""
    def __init__(self):
        super().__init__()
        self.edits = 0

    async def edit_stop(self, order_id, product_id, size, stop_price):
        self.edits += 1
        if self.edits == 1:
            raise DeltaError(400, {"error": "order_not_found"})
        return await super().edit_stop(order_id, product_id, size, stop_price)


@pytest.mark.asyncio
async def test_a_failed_amend_is_retried_before_replacing(tmp_path):
    fake = FlakyEdit()
    book = _book(tmp_path, fake)
    await _open_short(book)
    res = await book.on_stop_change("xrpusdt", 1.30)
    assert res["reason"] == "amended" and fake.edits == 2
    assert not any(c[0] == "cancel" for c in fake.calls)       # the retry worked: no replace needed


@pytest.mark.asyncio
async def test_reconcile_puts_back_a_missing_exchange_stop(tmp_path):
    fake = FakeDelta()
    fake.open_orders = lambda pid: _async([])                   # the exchange shows no stop order
    book = _book(tmp_path, fake)
    await _open_short(book)
    events = await book.reconcile()
    assert events[0]["event"] == "stop_restored" and fake.calls[-1][0] == "stop"


async def _async(v):
    return v


class ImmediateStop(FakeDelta):
    async def edit_stop(self, order_id, product_id, size, stop_price):
        raise DeltaError(400, {"error": {"code": "immediate_execution_stop_order"}})

    async def place_stop_market(self, product_id, side, size, stop_price):
        if self.calls and self.calls[-1][0] == "stop" or len([c for c in self.calls if c[0] == "stop"]) >= 1:
            raise DeltaError(400, {"error": {"code": "immediate_execution_stop_order"}})
        return await super().place_stop_market(product_id, side, size, stop_price)


@pytest.mark.asyncio
async def test_backstop_already_breached_closes_at_market(tmp_path):
    fake = ImmediateStop()
    book = _book(tmp_path, fake)
    await _open_short(book)
    res = await book.on_stop_change("xrpusdt", 1.30)
    assert res["reason"] == "closed_backstop_breached"
    assert any(c[0] == "market" and c[1]["reduce_only"] for c in fake.calls)
    assert "XRPUSD" not in book._load_state()["positions"]
