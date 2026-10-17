"""17 Oct 2026 review: Delta keeps leverage per product per account and a live account's default can be 100x, which
liquidates a position after ~1% - long before the engine's ~6% stop. Leverage is now set before every live entry so
liquidation sits >= 2x the stop distance away, size is capped at that leverage, and a fill whose exchange
liquidation price is inside the stop is closed at once."""
import pytest

from execution.delta_book import leverage_for
from execution.delta_india import DeltaError
from tests.test_delta_ladder import FakeDelta, _book, _open_short


class LevDelta(FakeDelta):
    def __init__(self, liq=None, fail_lev=False):
        super().__init__()
        self.liq, self.fail_lev, self.lev = liq, fail_lev, None

    async def set_leverage(self, product_id, leverage):
        if self.fail_lev:
            raise DeltaError(400, {"code": "leverage_error"})
        self.lev = leverage
        self.calls.append(("leverage", {"leverage": leverage}))
        return {"leverage": str(leverage)}

    async def liquidation_price(self, product_id):
        return self.liq


def test_leverage_keeps_liquidation_twice_as_far_as_the_stop():
    assert leverage_for(0.06) == 8
    assert leverage_for(0.03) == 10          # capped
    assert leverage_for(0.15) == 3
    for sf in (0.01, 0.03, 0.05, 0.08, 0.12, 0.2):
        lev = leverage_for(sf)
        assert 1 / lev - 0.005 >= 2 * sf or lev == 1


@pytest.mark.asyncio
async def test_leverage_is_set_before_the_entry(tmp_path):
    fake = LevDelta(liq=1.60)                 # short at 1.40, stop 1.47 (5%): liquidation 1.60 is beyond the stop
    book = _book(tmp_path, fake)
    plan = await _open_short(book)
    assert plan.ok and plan.leverage == leverage_for(0.05)
    kinds = [c[0] for c in fake.calls]
    assert kinds.index("leverage") < kinds.index("market")
    assert book._load_state()["positions"]["XRPUSD"]["leverage"] == plan.leverage


@pytest.mark.asyncio
async def test_no_order_when_leverage_cannot_be_set(tmp_path):
    fake = LevDelta(fail_lev=True)
    plan = await _open_short(_book(tmp_path, fake))
    assert not plan.ok and plan.reason == "leverage_not_set"
    assert not [c for c in fake.calls if c[0] == "market"]


@pytest.mark.asyncio
async def test_fill_with_liquidation_inside_the_stop_is_closed_at_once(tmp_path):
    fake = LevDelta(liq=1.45)                 # short, stop 1.47: liquidation 1.45 would hit first
    book = _book(tmp_path, fake)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason == "liquidation_inside_stop"
    assert any(c[0] == "market" and c[1].get("reduce_only") for c in fake.calls)
    assert "XRPUSD" not in (book._load_state().get("positions") or {})


@pytest.mark.asyncio
async def test_size_is_capped_by_the_chosen_leverage(tmp_path):
    # Rs500 wallet, 3% risk, 0.6% stop: risk sizing wants ~2,900 XRP; at 10x the wallet margins ~48 XRP
    fake = LevDelta(liq=2.0)
    book = _book(tmp_path, fake)
    plan = await book.on_paper_open("xrpusdt", "short", 1.40, 1.4084, 1.37, "t")
    assert plan.ok and plan.size * 1.40 <= (500 / 86.0) * plan.leverage * 0.9 + 1e-9


class StatusDelta(LevDelta):
    def __init__(self, status):
        super().__init__(liq=2.0)
        self.status = status

    async def trading_status(self, symbol):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


@pytest.mark.asyncio
async def test_no_order_when_the_market_is_cancel_only(tmp_path):
    # 17 Oct 2026: the whole demo exchange went 'disrupted_cancel_only'
    fake = StatusDelta("disrupted_cancel_only")
    plan = await _open_short(_book(tmp_path, fake))
    assert not plan.ok and plan.reason == "market_disrupted_cancel_only"
    assert not [c for c in fake.calls if c[0] in ("market", "leverage")]


@pytest.mark.asyncio
async def test_unreadable_market_status_fails_closed(tmp_path):
    fake = StatusDelta(DeltaError(500, {"error": "down"}))
    plan = await _open_short(_book(tmp_path, fake))
    assert not plan.ok and plan.reason == "market_unknown"


@pytest.mark.asyncio
async def test_operational_market_trades(tmp_path):
    assert (await _open_short(_book(tmp_path, StatusDelta("operational")))).ok
