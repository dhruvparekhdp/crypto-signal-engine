"""13 Oct 2026 smart-switch exits in the lab simulator: ladder, giveback, stale."""
import numpy as np
import pytest

from analysis.lab.costs import CostModel
from analysis.lab.data import Bars
from analysis.lab.simulate import ExitModel, simulate_symbol

ZERO = CostModel(taker_fee=0, maker_fee=0, gst=0, slip_bps=0, stop_slip_bps=0, funding=False)


def _bars(path, interval="1h"):
    n = len(path)
    t = (np.arange(n) * 3_600_000 + 1_700_000_000_000 // 3_600_000 * 3_600_000).astype(np.int64)
    p = np.asarray(path, float)
    return Bars("XUSDT", interval, t, p, p, p, p, np.ones(n), np.ones(n))


@pytest.fixture(autouse=True)
def _flat_atr(monkeypatch):
    import analysis.lab.simulate as S
    monkeypatch.setattr(S.F, "atr", lambda h, l, c, n=14: np.full(len(c), 1.0))   # ATR 1 -> stop 3, 1R = 3
    monkeypatch.setattr(S, "load_funding", lambda sym: (np.array([], np.int64), np.array([])))


def _one(path, exm):
    sig_bars = _bars([100.0] * 80 + list(path))
    sig = np.zeros(len(sig_bars.t), np.int8)
    sig[79] = 1                                     # long, entry at bar 80 open = 100
    return simulate_symbol(sig_bars, sig, sig_bars, exm, ZERO)[0]


def test_ladder_locks_half_r_after_one_and_a_half_r():
    exm = ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10_000, min_stop_pct=0, ladder=((1.0, 0.0), (1.5, 0.5)))
    r = _one([100, 103, 104.6, 104.6, 101.5], exm)        # best +4.6 (1.53R) -> stop 101.5 -> exits there
    assert r[16] == "trail" and r[5] == pytest.approx(101.5)


def test_giveback_exits_after_half_of_the_best_profit_is_gone():
    exm = ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10_000, min_stop_pct=0, giveback_after_r=1.0, giveback_frac=0.5)
    r = _one([100, 106, 103.0], exm)                       # best +6 (2R) -> stop 103 -> exits at 103
    assert r[5] == pytest.approx(103.0)


def test_stale_trade_closes_when_it_never_reached_one_r():
    exm = ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10_000, min_stop_pct=0, stale_after_min=180, stale_min_r=1.0)
    r = _one([100, 101, 101.5, 101.5, 101.5, 101.5], exm)
    assert r[16] == "stale" and r[5] == pytest.approx(101.5)


def test_old_exit_keys_are_unchanged():
    assert ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080).key() == "sl3.0_rr3.0_h10080_beNone_trNone_pNone"


def test_confirmed_lock_ignores_a_wick_but_exits_on_a_close():
    # ladder locks +0.5R (101.5 on a 100 long, 1R = 3) once 1.5R is reached; confirmation on 3-bar (3h) closes
    exm = ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=100_000, min_stop_pct=0, ladder=((1.5, 0.5),),
                    lock_confirm_min=180)
    sig = _bars([100.0] * 80 + [100, 104.6, 104.6, 101.0, 104.0, 104.0, 104.0, 104.0, 104.0, 104.0, 104.0, 108.0, 110.0])
    # the 101 bar (a wick through 101.5) is not a 3h close -> trade survives and reaches the 109 target
    from analysis.lab.simulate import simulate_symbol as sim
    s = np.zeros(len(sig.t), np.int8); s[79] = 1
    r = sim(sig, s, sig, exm, ZERO)[0]
    assert r[16] == "target"
    plain = ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=100_000, min_stop_pct=0, ladder=((1.5, 0.5),))
    assert sim(sig, s, sig, plain, ZERO)[0][16] == "trail"          # without confirmation the wick stops it out
