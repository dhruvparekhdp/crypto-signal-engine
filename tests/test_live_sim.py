"""Live simulator (20 Oct 2026): the swing book's live rules, using the live sizing code."""
import pandas as pd
import pytest

from scripts.live_sim import simulate

H = 3_600_000


def trades(rows):
    """rows: (entry_h, exit_h, coin, entry, stop_frac, net_ret, r)"""
    return pd.DataFrame([{"entry_t": a * H, "exit_t": b * H, "coin": c, "entry": e, "stop_frac": sf, "net_ret": nr,
                          "r_net": r, "side": -1, "spec": 0, "reason": "x", "filter_ok": True}
                         for a, b, c, e, sf, nr, r in rows])


def test_three_open_at_most_and_one_per_coin():
    t = trades([(0, 50, "ADA", 0.21, 0.07, 0.01, 0.14), (1, 50, "ADA", 0.21, 0.07, 0.01, 0.14),
                (2, 50, "XRP", 1.26, 0.05, 0.01, 0.2), (3, 50, "SUI", 0.97, 0.06, 0.01, 0.17),
                (4, 50, "TIA", 0.68, 0.07, 0.01, 0.14)])
    r = simulate(t, 500, 0.03)
    assert r["taken"] == 3 and r["skips"]["coin_open"] == 1 and r["skips"]["full"] == 1


def test_whole_contracts_with_the_round_up():
    # ADA at 0.21, 7% stop: one contract risks Rs1.25 -> 3% of Rs500 (Rs15) buys 12 contracts, risking ~Rs15
    r = simulate(trades([(0, 5, "ADA", 0.21, 0.07, -0.0712, -1.0)]), 500, 0.03, keep_trades=True)
    assert r["trades"][0]["contracts"] == 12 and r["trades"][0]["risk_inr"] == pytest.approx(15.0, abs=1.3)
    # DOGE: one contract (100 coins at 0.08, 6% stop) risks Rs40.8 = 8% of Rs500: above the 5% round-up -> skipped
    assert simulate(trades([(0, 5, "DOGE", 0.08, 0.06, 0.01, 0.2)]), 500, 0.03)["skips"]["too_small"] == 1
    # at Rs1,000 the same contract risks 4.1% -> the round-up takes one
    assert simulate(trades([(0, 5, "DOGE", 0.08, 0.06, 0.01, 0.2)]), 1000, 0.03)["taken"] == 1


def test_daily_loss_limit_stops_new_trades_for_the_day():
    t = trades([(0, 1, "ADA", 0.21, 0.07, -0.15, -2.1), (2, 9, "XRP", 1.26, 0.05, 0.01, 0.2),   # -21%, above the floor
                (30, 40, "SUI", 0.97, 0.06, 0.01, 0.17)])
    r = simulate(t, 500, 0.10)
    assert r["skips"]["daily_loss"] == 1 and r["taken"] == 2      # the next UTC day trades again
