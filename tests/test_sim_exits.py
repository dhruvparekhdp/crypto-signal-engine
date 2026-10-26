"""
Simulator exits match the live swing book (owner, 26 Oct 2026).

The old replay invented a 1.8×ATR / 1.2% 1R, ratcheted to break-even at
+0.8% (not 0.8R) and, in the batch engine, moved the stop by a percent
treated as a price. These tests pin the replacement: 3×ATR, the live
ladder, and the same result from a hand-built bar sequence.
"""
from analysis.simulator.exits import bracket, manage_bar, one_r
from analysis.simulator.replay_engine import SimulatedTrade
from analysis.swing_book import MIN_STOP_PCT, STOP_ATR


def _trade(entry=100.0, atr=1.0, side="LONG", tp_r=3.0, sl_r=1.0) -> SimulatedTrade:
    s = 1 if side == "LONG" else -1
    stop, target, _ = bracket(entry, atr, s, tp_r, sl_r)
    return SimulatedTrade(
        trade_id=1, symbol="BTCUSDT", direction=side,
        entry_time="t", entry_price=entry,
        tp_price=target, sl_price=stop, init_sl_price=stop,
    )


def test_one_r_is_the_live_swing_stop():
    # ATR 1 on a 100 price → 3×ATR = 3, which is above the 0.3% floor.
    assert one_r(100.0, 1.0) == STOP_ATR * 1.0
    # Tiny ATR falls back to 0.3% of price, same floor the live book uses.
    assert one_r(100.0, 0.01) == MIN_STOP_PCT * 100.0
    stop, target, unit = bracket(100.0, 1.0, 1, tp_r=3.0, sl_r=1.0)
    assert unit == 3.0
    assert stop == 97.0
    assert target == 109.0


def test_a_bar_that_hits_both_counts_as_the_stop():
    t = _trade()
    # stop 97, target 109. This bar trades through both.
    assert manage_bar(t, high=110, low=96, close=100, trail=True) == "stop"
    assert t.exit_reason == "STOP_LOSS"
    assert t.pnl_r == -1.0


def test_trail_locks_half_an_r_and_the_pullback_pays_it():
    t = _trade()                      # 1R = 3, stop 97, target 109
    # Close at 104.5 = +1.5R. Ladder locks +0.5R → stop at 100 + 0.5*3 = 101.5
    assert manage_bar(t, high=105, low=100, close=104.5, trail=True) is None
    assert t.sl_price == 101.5
    # Next bar pulls back through the lock and not the target.
    assert manage_bar(t, high=104, low=101, close=102, trail=True) == "stop"
    assert t.exit_reason == "TRAIL_STOP"
    assert t.pnl_r == 0.5


def test_trail_off_leaves_the_original_stop():
    t = _trade()
    assert manage_bar(t, high=105, low=100, close=104.5, trail=False) is None
    assert t.sl_price == 97.0
    assert manage_bar(t, high=104, low=96, close=99, trail=False) == "stop"
    assert t.exit_reason == "STOP_LOSS"
    assert t.pnl_r == -1.0


def test_the_close_cannot_move_the_stop_and_hit_it_on_the_same_bar():
    """Lock is for the next bar. A dip through break-even on the bar that
    first reaches 1R still uses the original stop... which this dip does not
    reach, so the trade stays open and the stop moves afterwards."""
    t = _trade()
    # High/close at +1.2R (103.6). Low at 99.5, above the original stop 97.
    assert manage_bar(t, high=103.6, low=99.5, close=103.6, trail=True) is None
    # 1R lock is break-even plus the round-trip cost, not a raw 100.
    assert t.sl_price > 100.0
