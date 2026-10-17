"""15 Oct 2026 12:25 UTC: the demo answered {'code': 'unavailable'} to one reconcile run -> three Telegram alerts and a
red go-live gate for 24 h. A read blip is retried, alerts only when it repeats, and does not count as an order error."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from execution.delta_india import DeltaError
from scheduler.golive_api import journal_stats
from tests.test_delta_ladder import FakeDelta, _book, _open_short


class BlipDelta(FakeDelta):
    def __init__(self, fails):
        super().__init__()
        self.fails = fails

    async def position_size(self, product_id):
        if self.fails > 0:
            self.fails -= 1
            raise DeltaError(400, {"code": "unavailable"})
        return self.size


@pytest.mark.asyncio
async def test_one_failed_read_is_retried_and_reconcile_is_clean(tmp_path):
    fake = BlipDelta(0)
    book = _book(tmp_path, fake)
    assert (await _open_short(book)).ok
    fake.fails = 1
    assert await book.reconcile() == []


@pytest.mark.asyncio
async def test_a_read_that_keeps_failing_is_reported(tmp_path):
    fake = BlipDelta(0)
    book = _book(tmp_path, fake)
    await _open_short(book)
    fake.fails = 5
    ev = await book.reconcile()
    assert [e["event"] for e in ev] == ["reconcile_error"]
    assert "XRPUSD" in book._load_state()["positions"]          # never dropped on a failed read


@pytest.mark.asyncio
async def test_alert_only_on_the_second_failed_run(monkeypatch):
    from scheduler.runner import AppRunner
    r = AppRunner.__new__(AppRunner)
    r.notifier = SimpleNamespace(send_text=AsyncMock())
    monkeypatch.setattr(AppRunner, "_delta_targets", staticmethod(lambda: ["live"]))
    err = [{"delta_symbol": "SOLUSD", "event": "reconcile_error", "error": "unavailable"}]
    r._delta_call_one = AsyncMock(return_value=err)
    await r._delta_reconcile_job()
    assert r.notifier.send_text.await_count == 0
    await r._delta_reconcile_job()
    assert r.notifier.send_text.await_count == 1
    r._delta_call_one = AsyncMock(return_value=[])                # recovered: counter resets
    await r._delta_reconcile_job()
    r._delta_call_one = AsyncMock(return_value=err)
    await r._delta_reconcile_job()
    assert r.notifier.send_text.await_count == 1


def test_read_blips_do_not_fail_the_order_error_gate():
    now = 1_792_100_000_000
    j = journal_stats([{"t": now - 60_000, "event": "reconcile_error", "error": "delta 400: unavailable"},
                       {"t": now - 30_000, "error": "delta 400: insufficient_margin"}], now)
    assert j["errors_24h"] == 1 and j["read_errors_24h"] == 1
