"""Limit entries in the lab (owner, 18 Oct 2026: scalping research needs maker fills, tested conservatively)."""
import numpy as np
import pytest

from analysis.lab.costs import PRESETS, CostModel
from analysis.lab.simulate import ExitModel, simulate_symbol
from tests.test_lab_engine import NO_COST, bars, flat

SIG = 60
LIM = ExitModel(stop_atr=10.0, rr=1.0, min_stop_pct=0.0, max_stop_pct=1.0, entry_limit_bps=0.0, entry_window_min=5)


def _run(rows, exit=LIM, cost=NO_COST, side=1):
    b = bars(rows)
    sig = np.zeros(len(b), np.int8)
    sig[SIG] = side
    return simulate_symbol(b, sig, b, exit, cost)


ABOVE = [(100.02, 100.07, 100.0, 100.02)]                       # lows touch 100.00, never below


def test_a_limit_that_is_only_touched_does_not_fill():
    rows = flat(61) + ABOVE * 40                                  # signal close 100.00 at bar 60
    assert _run(rows) == []


def test_a_limit_traded_through_fills_at_the_limit_with_no_slippage():
    rows = flat(62) + [(100, 100.05, 99.90, 99.95)] + flat(200, 100.0)
    t = _run(rows, cost=CostModel(gst=0.0, slip_bps=5.0, stop_slip_bps=0.0, funding=False))
    assert len(t) == 1 and t[0][4] == pytest.approx(100.0)        # the signal close, not the open + slippage


def test_after_the_window_the_order_is_cancelled():
    rows = flat(61) + ABOVE * 9 + [(100, 100.05, 99.5, 99.6)] + flat(200, 99.6)   # dips through 10 min after
    assert _run(rows) == []                                                     # window is 5 minutes


def test_the_stop_can_hit_on_the_fill_bar_but_the_target_cannot():
    # 1R = 1% (stop_atr 10 x ATR 0.1). Fill bar trades from 99.95 down to 98.9: stop (99.0) hit on the fill bar.
    rows = flat(61) + [(100, 100.05, 98.9, 99.0)] + flat(200, 99.0)
    t = _run(rows)
    assert len(t) == 1 and t[0][16] == "stop"
    # fill bar spans 99.9..101.2: the target (101.0) is NOT credited on the fill bar; next bars flat -> time exit
    rows = flat(61) + [(100, 101.2, 99.9, 101.0)] + flat(200, 100.5)
    t = _run(rows, exit=ExitModel(stop_atr=10.0, rr=1.0, min_stop_pct=0.0, max_stop_pct=1.0, entry_limit_bps=0.0,
                                  entry_window_min=5, max_hold_min=30))
    assert len(t) == 1 and t[0][16] == "time"


def test_a_limit_entry_pays_the_maker_fee_and_a_market_entry_the_taker_fee():
    rows = flat(62) + [(100, 100.05, 99.90, 99.95)] + flat(200, 100.0)
    cost = CostModel(gst=0.0, slip_bps=0.0, stop_slip_bps=0.0, funding=False)
    lim = _run(rows, cost=cost)[0]
    mkt = _run(rows, exit=ExitModel(stop_atr=10.0, rr=1.0, min_stop_pct=0.0, max_stop_pct=1.0), cost=cost)[0]
    assert lim[8] == pytest.approx(cost.maker + cost.taker)          # maker in, taker out (time exit)
    assert mkt[8] == pytest.approx(2 * cost.taker)


def test_a_short_limit_mirrors_a_long():
    rows = flat(62) + [(100, 100.10, 99.95, 100.05)] + flat(200, 100.0)
    t = _run(rows, side=-1)
    assert len(t) == 1 and t[0][1] == -1 and t[0][4] == pytest.approx(100.0)


def test_delta_maker_preset_matches_delta_india_fees():
    c = PRESETS["delta_maker"]
    assert (c.maker_fee, c.taker_fee, c.gst, c.tp_maker) == (0.0002, 0.0005, 0.18, True)
    assert LIM.key().endswith("_lim0w5")
