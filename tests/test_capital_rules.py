"""Kill rule + profit-withdrawal rule (owner 30 Oct 2026): enforce only from Rs10,000 put in; test mode below."""
import time

import pytest

from execution import capital_rules as cr

T0 = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, 0)) - time.timezone      # 30 Oct 2026 12:00 UTC


def run(rules, wallet, net, trades, now=T0, ack=""):
    return cr.evaluate(rules, wallet_inr=wallet, net_inr=net, live_trades=trades, now=now, ack=ack)


def test_at_rs800_the_kill_rule_only_reports_and_never_blocks():
    r, ev = run(None, 800, 800, 5)
    assert r["enforcing"] is False and ev == []
    r, ev = run(r, 700, 800, 105)                       # 100 trades later, down Rs100
    assert ev[0]["event"] == "kill_rule_tripped" and ev[0]["enforcing"] is False and ev[0]["trades"] == 100
    assert cr.blocks_entries(r) == "" and "TEST MODE" in cr.alert_text(ev[0])
    assert run(r, 650, 800, 120)[1] == []               # reported once


def test_deposits_never_count_as_profit_and_100_trades_are_needed():
    r, _ = run(None, 20_000, 20_000, 0)
    r, ev = run(r, 29_000, 30_000, 99)                  # added Rs10k, down Rs1k over 99 trades: not judged yet
    assert ev == [] and r["kill"]["profit"] == -1000
    r, ev = run(r, 29_000, 30_000, 100)
    assert ev[0]["event"] == "kill_rule_tripped" and cr.blocks_entries(r) == "kill_rule"
    r2, _ = run(r, 31_000, 30_000, 100)                 # recovering does not clear it: only the owner does
    assert cr.blocks_entries(r2) == "kill_rule"
    r3, ev = run(r2, 31_000, 30_000, 101, ack=r["kill"]["trip_id"])
    assert ev[0]["event"] == "kill_rule_cleared" and cr.blocks_entries(r3) == "" and r3["kill"]["trades"] == 0


def test_reaching_rs10k_restarts_the_count_so_only_real_size_trades_are_judged():
    r, _ = run(None, 800, 800, 90)
    r, ev = run(r, 10_500, 10_800, 95)                  # owner adds Rs10k
    assert ev[0] == {"event": "capital_rules_mode", "enforcing": True, "net_inr": 10_800}
    assert r["kill"]["trades"] == 0 and r["kill"]["profit"] == 0
    r, ev = run(r, 10_000, 10_800, 194)
    assert ev == []                                     # 99 real-size trades so far


def test_half_of_last_months_profit_is_held_back_until_withdrawn():
    nov = T0 + 3 * 86400                                # 2 Nov
    r, _ = run(None, 20_000, 20_000, 0)
    r, ev = run(r, 22_000, 20_000, 10, now=nov)         # October made Rs2,000
    assert ev == [{"event": "profit_withdrawal_due", "enforcing": True, "month": "2026-10", "profit_inr": 2000,
                   "withdraw_inr": 1000}]
    assert cr.reserve_inr(r) == 1000
    r, ev = run(r, 21_500, 19_500, 12, now=nov + 3600)  # withdrew Rs500
    assert cr.reserve_inr(r) == 500 and ev == []
    r, ev = run(r, 21_000, 19_000, 12, now=nov + 7200)  # and the rest
    assert cr.reserve_inr(r) == 0 and ev == [{"event": "profit_withdrawn"}]


def test_a_losing_month_asks_for_nothing_and_test_mode_holds_nothing():
    dec = T0 + 40 * 86400
    r, _ = run(None, 800, 800, 0)
    r, ev = run(r, 900, 800, 3, now=dec)                # test mode, profit Rs100
    assert ev[0]["withdraw_inr"] == 50 and cr.reserve_inr(r) == 0 and "TEST MODE" in cr.alert_text(ev[0])
    r, ev = run(None, 20_000, 20_000, 0)
    r, ev = run(r, 19_000, 20_000, 3, now=dec)
    assert ev[0]["withdraw_inr"] == 0 and cr.reserve_inr(r) == 0


@pytest.mark.asyncio
async def test_the_live_book_refuses_entries_only_when_the_rule_enforces(tmp_path):
    from tests.test_delta_ladder import FakeDelta, _book
    d = FakeDelta()

    async def total():
        return 500.0
    d.wallet_total_inr = total
    book = _book(tmp_path, d, rules_from_inr=10_000)
    st = book._load_state()
    st["capital_rules"] = {"enforcing": False, "kill": {"tripped": True}}
    book._save_state(st)
    assert await book._loss_guard() != "kill_rule"
    st["capital_rules"]["enforcing"] = True
    book._save_state(st)
    assert await book._loss_guard() == "kill_rule"
    plan = await book.on_paper_open("xrpusdt", "short", 1.40, 1.47, 1.19, "swing_donchian")
    assert not plan.ok and plan.reason == "kill_rule"


@pytest.mark.asyncio
async def test_the_five_minute_check_counts_past_live_trades_and_saves_the_rules(tmp_path):
    import json

    from tests.test_delta_ladder import FakeDelta, _book
    d = FakeDelta()

    async def total():
        return 790.0
    d.wallet_total_inr = total
    book = _book(tmp_path, d, rules_from_inr=10_000)
    (tmp_path / "shadow.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"mode": "live", "placed": True, "size": 11}, {"mode": "live", "placed": True, "size": 2},
        {"mode": "live", "placed": False, "size": 0}, {"mode": "live", "event": "close", "placed": True}]))
    st = book._load_state()
    st["capital"] = {"net_inr": 800.0}
    book._save_state(st)
    assert await book.check_capital_rules() == []
    st = book._load_state()
    assert st["capital_rules_trades"] == 2 and st["capital_rules"]["enforcing"] is False
    assert st["capital_rules"]["kill"]["profit_at_start"] == -10.0
