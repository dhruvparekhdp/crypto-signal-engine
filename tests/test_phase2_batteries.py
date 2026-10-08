"""Phase 2.5 batteries (plan Q7, Q9, Q10): one trade costs the same everywhere; bad data and nonsense inputs are
refused instead of silently priced. (Known-answer and causality tests live in tests/test_lab_engine.py.)"""
import numpy as np
import pandas as pd
import pytest

from analysis.lab.costs import PRESETS, CostModel
from analysis.lab.data import INTERVAL_MS, Bars, MissingData, check_clock
from analysis.paper_cycle import CycleConfig, stop_out_costs
from analysis.paper_trading import FeeModel


def test_lab_and_paper_charge_the_same_for_a_stop_out():
    lab = PRESETS["india_gst"]
    lab_stop_out = 2 * lab.taker + 2 * lab.slip_bps / 1e4 + lab.stop_slip_bps / 1e4
    assert stop_out_costs("SOLUSDT", CycleConfig()) == pytest.approx(lab_stop_out)        # 0.188% today
    assert FeeModel().effective_taker_pct == pytest.approx(lab.taker)


@pytest.mark.parametrize("bad", [dict(taker_fee=-0.0005), dict(slip_bps=-1), dict(gst=1.8)])
def test_lab_cost_model_refuses_nonsense(bad):
    with pytest.raises(ValueError):
        CostModel(**bad)


@pytest.mark.parametrize("bad", [dict(taker_pct=-0.0005), dict(taker_pct=0.5), dict(gst_pct=18)])
def test_paper_fee_model_refuses_nonsense(bad):
    with pytest.raises(ValueError):
        FeeModel(**bad)


def test_lake_clock_must_be_epoch_ms_on_the_grid():
    ms = pd.Series(np.arange(5) * INTERVAL_MS["1h"] + 1_700_000_000_000 - 1_700_000_000_000 % INTERVAL_MS["1h"])
    check_clock(ms, "1h")                                            # fine
    with pytest.raises(MissingData):
        check_clock(ms // 1000, "1h")                                # seconds, not ms
    with pytest.raises(MissingData):
        check_clock(ms + 1234, "1h")                                 # off the hour grid


def _bars(close):
    n = len(close)
    t = 1_700_000_000_000 - 1_700_000_000_000 % 14_400_000 + np.arange(n) * 14_400_000
    c = np.asarray(close, float)
    return Bars("SOLUSDT", "4h", t.astype(np.int64), c, c * 1.01, c * 0.99, c, np.ones(n), np.ones(n) / 2)


def test_swing_signal_on_bad_prints_is_none_not_nan():
    # zero print, frozen price and a spike: the swing evaluator must never hand back a NaN stop or signal
    from analysis import swing_book as sb
    from analysis.lab.strategies import REGISTRY
    rng = np.random.default_rng(3)
    base = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    for bad in (np.r_[base[:-1], 0.0], np.r_[base[:200], np.full(200, base[199])], np.r_[base[:-1], base[-2] * 50]):
        b = _bars(bad)
        for sid in ("vol_breakout", "keltner_break", "donchian", "ichimoku"):
            with np.errstate(all="ignore"):
                sig = REGISTRY[sid].signals(b, {})
            assert not np.isnan(sig.astype(float)).any()
            with np.errstate(all="ignore"):
                setup = sb.evaluate(b, [("4h", sid, {})])
            if setup is not None:                      # a signal on bad data must price sane levels or none
                from datetime import UTC, datetime
                sig = sb.to_signal(setup, setup.close, datetime.now(UTC))
                if sig is not None:
                    assert np.isfinite(sig.stop_loss) and sig.stop_loss > 0, (sid, setup)
                    assert np.isfinite(sig.target) and sig.target > 0, (sid, setup)


def test_new_filters_are_causal_and_sane():
    from analysis.lab import features as F
    rng = np.random.default_rng(5)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 600)))
    o, h, l = c * (1 + rng.normal(0, 0.002, 600)), c * 1.01, c * 0.99
    for fn, args in ((F.kama, (c,)), (F.mcginley, (c,)), (F.garman_klass, (o, h, l, c))):
        full = fn(*args)
        cut = fn(*(a[:400] for a in args))
        assert np.allclose(full[:400], cut, equal_nan=True), fn.__name__    # no value depends on later bars
        assert np.isfinite(full[-1]) and full[-1] > 0
    trend = np.linspace(100, 200, 300)
    k = F.kama(trend)
    assert k[-1] > k[-50]                                                 # follows a steady trend upward
