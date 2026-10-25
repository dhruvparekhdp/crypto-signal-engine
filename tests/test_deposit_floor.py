"""Floor = 50% of the money put in (deposits - withdrawals on Delta), never moved by wins or losses (26 Oct 2026)."""
import time

import pytest

from execution.delta_book import DeltaBook
from execution.delta_india import DeltaError, DeltaIndia


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, ""

    def json(self):
        return self._d


class _Http:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    async def request(self, method, url, content=None, headers=None, timeout=None):
        self.calls.append(url)
        return _Resp(self.pages.pop(0))


async def test_capital_flows_reads_only_money_in_and_out_across_pages():
    http = _Http([
        {"success": True, "meta": {"after": "c2"}, "result": [
            {"id": 1, "transaction_type": "deposit", "amount": "5.88", "asset_symbol": "USD", "created_at": "2026-10-17T10:00:00Z"},
            {"id": 2, "transaction_type": "pnl", "amount": "-0.13", "asset_symbol": "USD", "created_at": "2026-10-20T10:00:00Z"},
            {"id": 3, "transaction_type": "commission", "amount": "-0.01", "asset_symbol": "USD", "created_at": "2026-10-20T10:00:00Z"}]},
        {"success": True, "meta": {"after": None}, "result": [
            {"id": 4, "transaction_type": "withdrawal", "amount": "1.18", "asset_symbol": "USD", "created_at": 1792900000000000},
            {"id": 5, "transaction_type": "withdrawal_cancellation", "amount": "1.18", "asset_symbol": "USD", "created_at": 1792900001000000}]}])
    flows = await DeltaIndia("k", "s", client=http).capital_flows()
    assert [(f["id"], f["type"], f["amount"]) for f in flows] == [("1", "deposit", 5.88), ("4", "withdrawal", -1.18),
                                                                 ("5", "withdrawal_cancellation", 1.18)]
    assert flows[0]["t_us"] == 1792231200000000 and flows[1]["t_us"] == 1792900000000000
    assert "after=c2" in http.calls[1] and "/v2/wallet/transactions" in http.calls[0]


class _Client:
    def __init__(self, flows, wallet=1000.0):
        self.flows, self.wallet, self.fail = list(flows), wallet, False

    async def capital_flows(self, since_us=None):
        if self.fail:
            raise DeltaError(503, {"error": "unavailable"})
        return [f for f in self.flows if since_us is None or f["t_us"] >= since_us]

    async def wallet_total_inr(self):
        return self.wallet


def _flow(i, typ, usd, t_us=None):
    return {"id": str(i), "type": typ, "asset": "USD", "amount": usd, "t_us": t_us or 1792231200000000}


def _book(tmp_path, client, frac=0.5, mode="live"):
    return DeltaBook(client, mode=mode, live_orders=True, usd_inr=85.0, state_path=tmp_path / "state.json",
                     shadow_path=tmp_path / "shadow.jsonl", min_wallet_inr=250.0, daily_loss_pct=0.10, floor_frac=frac)


async def test_the_floor_is_half_the_money_put_in_and_follows_deposits_and_withdrawals(tmp_path):
    c = _Client([_flow(1, "deposit", 500 / 85), _flow(2, "deposit", 300 / 85)])
    b = _book(tmp_path, c)
    assert b.floor_inr() == 250.0                                   # before deposits are read: the fixed floor
    ev = await b.refresh_capital()
    assert ev["floor_inr"] == pytest.approx(400.0) and ev["net_deposits_inr"] == pytest.approx(800.0)
    assert b.floor_inr() == pytest.approx(400.0)
    assert await b.refresh_capital() is None                         # nothing new: no alert
    # a withdrawal today lowers the floor and the day's starting wallet (a withdrawal is not a trading loss)
    now_us = int(time.time() * 1_000_000)
    st = b._load_state()
    st["day"] = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "start_inr": 800.0}
    b._save_state(st)
    c.flows.append(_flow(3, "withdrawal", -100 / 85, now_us))
    ev = await b.refresh_capital()
    assert ev["floor_inr"] == pytest.approx(350.0) and ev["was_inr"] == pytest.approx(400.0)
    assert b._load_state()["day"]["start_inr"] == pytest.approx(700.0)


async def test_wins_and_losses_never_move_it_and_the_guard_uses_it(tmp_path):
    c = _Client([_flow(1, "deposit", 800 / 85)], wallet=390.0)
    b = _book(tmp_path, c)
    await b.refresh_capital()
    assert await b._loss_guard() == "wallet_floor"                   # Rs390 < Rs400 (half of Rs800 put in)
    c.wallet = 1200.0                                                # a big win does not raise the floor
    assert await b.refresh_capital() is None and b.floor_inr() == pytest.approx(400.0)
    assert await b._loss_guard() == ""


async def test_an_unreadable_history_keeps_the_floor_and_off_or_demo_never_reads_it(tmp_path):
    c = _Client([_flow(1, "deposit", 800 / 85)])
    b = _book(tmp_path, c)
    await b.refresh_capital()
    c.fail = True
    assert await b.refresh_capital() is None and b.floor_inr() == pytest.approx(400.0)
    assert await _book(tmp_path / "x", _Client([_flow(1, "deposit", 10)]), frac=0.0).refresh_capital() is None
    assert await _book(tmp_path / "y", _Client([_flow(1, "deposit", 10)]), mode="shadow").refresh_capital() is None
    nothing = _book(tmp_path / "z", _Client([]))
    assert await nothing.refresh_capital() is None and nothing.floor_inr() == 250.0


async def test_the_reconcile_job_sends_a_plain_floor_alert(monkeypatch):
    from unittest.mock import AsyncMock

    from config.settings import settings
    from scheduler.runner import AppRunner
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_testnet_mirror", False)
    r = AppRunner()
    r.notifier.send_text = AsyncMock(return_value=True)

    async def one(target, method, *a, **k):
        return [{"event": "floor_updated", "floor_inr": 400.0, "was_inr": 250.0, "net_deposits_inr": 800.0,
                 "new_flows": [{"type": "deposit", "inr": 300.0}]}]
    r._delta_call_one = one
    await r._delta_reconcile_job()
    text = r.notifier.send_text.call_args.args[0]
    assert "floor now ₹400.00" in text and "₹800.00 put in" in text and "deposit ₹+300.00" in text


async def test_opening_capital_counts_once_with_deposits_after_the_count_from_date(tmp_path):
    # 26 Oct 2026 on production: Delta listed the Rs300 deposit of 25 Oct but not the opening Rs500 of 17 Oct
    opening = _flow(1, "deposit", 500 / 85, 1792231200000000)                 # 17 Oct - inside the base
    later = _flow(2, "deposit", 300 / 85, 1792900000000000)                   # 25 Oct
    b = DeltaBook(_Client([opening, later]), mode="live", live_orders=True, usd_inr=85.0,
                  state_path=tmp_path / "s.json", shadow_path=tmp_path / "sh.jsonl", min_wallet_inr=250.0,
                  floor_frac=0.5, capital_base_inr=500.0, capital_since="2026-10-18")
    ev = await b.refresh_capital()
    assert ev["net_deposits_inr"] == pytest.approx(800.0) and ev["floor_inr"] == pytest.approx(400.0)
    only_base = DeltaBook(_Client([]), mode="live", live_orders=True, usd_inr=85.0, state_path=tmp_path / "t.json",
                          shadow_path=tmp_path / "th.jsonl", floor_frac=0.5, capital_base_inr=500.0)
    assert (await only_base.refresh_capital())["floor_inr"] == pytest.approx(250.0)


def test_count_from_date_is_utc_midnight():
    from execution.delta_book import _day_us
    assert _day_us("2026-10-18") == 1792281600 * 1_000_000 and _day_us("") == 0 and _day_us("18/10") == 0
