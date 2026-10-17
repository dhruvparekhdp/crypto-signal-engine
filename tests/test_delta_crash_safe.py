"""18 Oct 2026: a live position must never sit on Delta without a stop and without our record - not after a restart
between the entry and its stop, and not when Delta rejects the stop."""
import json
import time

import pytest

from execution.delta_india import DeltaError
from tests.test_delta_ladder import FakeDelta, _book, _open_short


class RejectStop(FakeDelta):
    def __init__(self, fails=2):
        super().__init__()
        self.fails = fails

    async def place_stop_market(self, product_id, side, size, stop_price):
        if self.fails > 0:
            self.fails -= 1
            raise DeltaError(400, {"code": "invalid_stop"})
        return await super().place_stop_market(product_id, side, size, stop_price)


@pytest.mark.asyncio
async def test_rejected_stop_is_retried_once(tmp_path):
    fake = RejectStop(fails=1)
    book = _book(tmp_path, fake)
    plan = await _open_short(book)
    assert plan.ok and book._load_state()["positions"]["XRPUSD"]["stop_order_id"]


@pytest.mark.asyncio
async def test_stop_rejected_twice_means_flat_not_unprotected(tmp_path):
    fake = RejectStop(fails=2)
    book = _book(tmp_path, fake)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason == "stop_rejected_closed"
    assert fake.size == 0                                           # closed at market
    assert "XRPUSD" not in (book._load_state().get("positions") or {})


@pytest.mark.asyncio
async def test_record_exists_before_the_entry_is_sent(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    seen = {}
    real = fake.place_market

    async def spy(product_id, side, size, reduce_only=False):
        if not reduce_only:
            seen["record_at_entry"] = book._load_state()["positions"].get("XRPUSD")
        return await real(product_id, side, size, reduce_only)
    fake.place_market = spy
    await _open_short(book)
    assert seen["record_at_entry"]["pending"] is True and seen["record_at_entry"]["exchange_stop"] == 1.47
    assert book._load_state()["positions"]["XRPUSD"]["pending"] is False


@pytest.mark.asyncio
async def test_restart_after_entry_reconcile_restores_the_stop(tmp_path):
    # simulate the crash: record saved + entry filled, then the process died before the stop
    fake = FakeDelta()
    fake.size = -5
    fake.open_orders = lambda product_id: _none()
    book = _book(tmp_path, fake)
    rec = {"side": "sell", "size": 5, "entry": 1.40, "stop": 1.47, "init_stop": 1.47, "exchange_stop": 1.47,
           "risk": 0.07, "target": 1.19, "product_id": 14969, "tick": 0.0001, "binance_symbol": "xrpusdt",
           "opened_ms": int(time.time() * 1000) - 600_000, "pending": True, "entry_fill": "1.40"}
    (tmp_path / "state.json").write_text(json.dumps({"positions": {"XRPUSD": rec}}))
    ev = await book.reconcile()
    assert any(e["event"] == "stop_restored" for e in ev)
    assert any(c[0] == "stop" and c[1]["stop_price"] == 1.47 for c in fake.calls)


@pytest.mark.asyncio
async def test_entry_that_never_filled_is_dropped_quietly(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    rec = {"side": "sell", "size": 5, "product_id": 14969, "pending": True, "opened_ms": int(time.time() * 1000) - 600_000,
           "exchange_stop": 1.47}
    (tmp_path / "state.json").write_text(json.dumps({"positions": {"XRPUSD": rec}}))
    ev = await book.reconcile()
    assert [e["event"] for e in ev] == ["entry_never_filled"] and not book._load_state()["positions"]


@pytest.mark.asyncio
async def test_an_entry_in_flight_is_not_touched_by_reconcile(tmp_path):
    fake = FakeDelta()
    book = _book(tmp_path, fake)
    rec = {"side": "sell", "size": 5, "product_id": 14969, "pending": True, "opened_ms": int(time.time() * 1000),
           "exchange_stop": 1.47}
    (tmp_path / "state.json").write_text(json.dumps({"positions": {"XRPUSD": rec}}))
    assert await book.reconcile() == [] and "XRPUSD" in book._load_state()["positions"]


@pytest.mark.asyncio
async def test_failed_entry_leaves_no_record(tmp_path):
    fake = FakeDelta()

    async def boom(*a, **k):
        raise DeltaError(400, {"code": "insufficient_margin"})
    fake.place_market = boom
    book = _book(tmp_path, fake)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason.startswith("order_failed")
    assert not (book._load_state().get("positions") or {})


async def _none():
    return []
