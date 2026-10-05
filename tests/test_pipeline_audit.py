"""The multi-year pipeline's audit counts each trade once, uses real R, and charges costs."""
from scripts.backtest_multi_year_pipeline import ROUND_TRIP_COST_PCT, audit_all_simulated_trades, trade_r, unique_trades


def tp(t="2025-01-01", pnl=2.0):
    return {"symbol": "BTCUSDT", "direction": "LONG", "entry_time": t, "entry_price": 100.0, "sl": 98.8,
            "pnl_pct": pnl, "exit_reason": "TAKE_PROFIT_2.0R"}


def sl(t="2025-01-02"):
    return {"symbol": "BTCUSDT", "direction": "LONG", "entry_time": t, "entry_price": 100.0, "sl": 98.8,
            "pnl_pct": -1.2, "exit_reason": "STOP_LOSS_1.2R"}


def test_nested_windows_do_not_count_a_trade_five_times():
    one = [tp(), sl()]
    audit = audit_all_simulated_trades(one * 5)            # the same two trades seen in five windows
    assert audit["total_trades_audited"] == 2
    assert audit["duplicates_removed"] == 8
    assert len(unique_trades(one * 5)) == 2


def test_r_comes_from_prices_and_includes_costs():
    assert abs(trade_r(tp(), 0.0) - 2.0 / 1.2) < 1e-9         # +1.67 R before costs, not +2
    assert abs(trade_r(sl(), 0.0) + 1.0) < 1e-9
    assert trade_r(tp()) < trade_r(tp(), 0.0)
    assert abs(trade_r(sl()) - (-1.2 - ROUND_TRIP_COST_PCT) / 1.2) < 1e-9


def test_win_rate_and_total_r_are_net():
    a = audit_all_simulated_trades([tp(), sl(), sl("2025-01-03")])
    assert a["win_rate"] == round(100 / 3, 2)
    expected = trade_r(tp()) + 2 * trade_r(sl())
    assert abs(a["total_r_multiple"] - round(expected, 1)) < 1e-9
    assert a["net_of_costs"] is True
