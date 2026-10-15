"""Delta book automatic loss guard (15 Oct 2026, before the Rs500 real trial): no new orders after a bad day or
below the wallet floor; open positions are never touched. Fake exchange: no network, no real orders."""
import pytest

from execution.delta_india import DeltaError
from tests.test_delta_ladder import FakeDelta, _book, _open_short


class WalletDelta(FakeDelta):
    def __init__(self, wallet=500.0, fail=False):
        super().__init__()
        self.wallet, self.fail = wallet, fail

    async def wallet_total_inr(self):
        if self.fail:
            raise DeltaError(500, {"error": "down"})
        return self.wallet


@pytest.mark.asyncio
async def test_no_new_order_after_the_daily_loss_limit(tmp_path):
    fake = WalletDelta(500.0)
    book = _book(tmp_path, fake, daily_loss_pct=0.10)
    assert (await book.reconcile()) == []                 # 5-minute job records today's start: Rs500
    fake.wallet = 445.0                                   # lost Rs55 today (> Rs50)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason == "daily_loss_limit"
    assert not [c for c in fake.calls if c[0] == "market"]


@pytest.mark.asyncio
async def test_small_loss_still_trades(tmp_path):
    fake = WalletDelta(500.0)
    book = _book(tmp_path, fake, daily_loss_pct=0.10)
    await book.reconcile()
    fake.wallet = 460.0
    assert (await _open_short(book)).ok


@pytest.mark.asyncio
async def test_wallet_floor_blocks_new_orders(tmp_path):
    book = _book(tmp_path, WalletDelta(340.0), min_wallet_inr=350.0)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason == "wallet_floor"


@pytest.mark.asyncio
async def test_unreadable_wallet_fails_closed(tmp_path):
    book = _book(tmp_path, WalletDelta(fail=True), daily_loss_pct=0.10)
    plan = await _open_short(book)
    assert not plan.ok and plan.reason == "wallet_unreadable"


@pytest.mark.asyncio
async def test_guard_never_closes_open_positions(tmp_path):
    fake = WalletDelta(500.0)
    book = _book(tmp_path, fake, daily_loss_pct=0.10)
    assert (await _open_short(book)).ok
    fake.wallet = 300.0
    await book.reconcile()
    assert "XRPUSD" in book._load_state()["positions"]
    assert not [c for c in fake.calls if c[0] == "market" and c[1].get("reduce_only")]


@pytest.mark.asyncio
async def test_new_utc_day_resets_the_start(tmp_path, monkeypatch):
    import execution.delta_book as db
    fake = WalletDelta(500.0)
    book = _book(tmp_path, fake, daily_loss_pct=0.10)
    await book.reconcile()
    fake.wallet = 440.0
    assert (await _open_short(book)).reason == "daily_loss_limit"
    monkeypatch.setattr(db.time, "strftime", lambda fmt, t=None: "2099-01-01")
    assert (await _open_short(book)).ok                   # tomorrow starts at Rs440


@pytest.mark.asyncio
async def test_wallet_total_counts_margin_in_open_positions():
    from execution.delta_india import DeltaIndia

    class Fake(DeltaIndia):
        def __init__(self):
            pass

        async def balances(self):
            # USD 100 wallet, 40 of it held as margin: available 60 = Rs5,100 -> whole wallet Rs8,500
            return [{"asset_symbol": "USD", "balance": "100", "available_balance": "60", "available_balance_inr": "5100"}]
    assert await Fake().wallet_total_inr() == pytest.approx(8500.0)
