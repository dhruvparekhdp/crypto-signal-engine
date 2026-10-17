"""Swing research strategies (owner, 18 Oct 2026): relative strength vs BTC, KAMA trend, momentum with volume,
EMA ribbon, breakouts confirmed by Supertrend, and the five-strategy swing votes. Synthetic bars only (no lake)."""
import numpy as np
import pytest

from analysis.lab import strategies as S
from analysis.lab.data import INTERVAL_MS, Bars

H4 = INTERVAL_MS["4h"]
NEW = ("rel_strength", "kama_trend", "roc_volume", "ema_ribbon", "breakout_confirmed", "vote2_swing", "vote3_swing")


def _bars(c, symbol="ETHUSDT", v=None):
    c = np.asarray(c, float)
    n = len(c)
    return Bars(symbol, "4h", np.arange(n, dtype=np.int64) * H4 + 1_700_000_000_000,
                c, c * 1.01, c * 0.99, c, np.full(n, 100.0) if v is None else np.asarray(v, float), np.full(n, 50.0))


@pytest.fixture
def flat_btc(monkeypatch):
    """BTC flat at 100 on every 4h bar, so a coin's relative strength is its own return."""
    t = np.arange(2000, dtype=np.int64) * H4 + 1_700_000_000_000
    monkeypatch.setitem(S._BTC_CLOSE, "4h", (t, np.full(2000, 100.0)))


def _walk(n=900, seed=1):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))


def test_new_strategies_and_their_mirrors_are_registered():
    for sid in NEW:
        assert sid in S.REGISTRY
    for sid in NEW[:5]:
        assert f"mirror_{sid}" in S.REGISTRY


def test_relative_strength_goes_long_when_the_coin_beats_btc(flat_btc):
    c = np.r_[np.full(100, 100.0), np.linspace(100, 130, 60)]       # +30% while BTC is flat
    sig = S.REGISTRY["rel_strength"].signals(_bars(c), {"n": 42, "thr": 0.08})
    assert (sig > 0).sum() >= 1 and (sig < 0).sum() == 0


def test_relative_strength_goes_short_when_the_coin_lags_btc(flat_btc):
    c = np.r_[np.full(100, 100.0), np.linspace(100, 75, 60)]
    sig = S.REGISTRY["rel_strength"].signals(_bars(c), {"n": 42, "thr": 0.08})
    assert (sig < 0).sum() >= 1 and (sig > 0).sum() == 0


def test_relative_strength_never_trades_btc_against_itself(flat_btc):
    sig = S.REGISTRY["rel_strength"].signals(_bars(_walk(), "BTCUSDT"))
    assert not sig.any()


def test_kama_trend_needs_the_average_sloping_with_the_cross():
    c = np.r_[np.linspace(120, 100, 150), np.linspace(100, 140, 150)]   # down then up
    sig = S.REGISTRY["kama_trend"].signals(_bars(c))
    assert (sig[150:] > 0).any()                                          # the turn up is taken long
    assert not (sig[150:] < 0).any()                                      # no short inside the up leg


def test_momentum_needs_above_normal_volume():
    c = np.r_[np.full(200, 100.0), np.linspace(100, 115, 12), np.full(50, 115.0)]
    quiet = S.REGISTRY["roc_volume"].signals(_bars(c))
    v = np.full(len(c), 100.0)
    v[200:212] = 400.0
    loud = S.REGISTRY["roc_volume"].signals(_bars(c, v=v))
    assert not quiet.any()
    assert (loud > 0).sum() == 1                                          # first bar of the condition only


def test_ema_ribbon_fires_once_when_the_lines_line_up():
    c = np.r_[np.full(200, 100.0), np.linspace(100, 160, 200)]
    sig = S.REGISTRY["ema_ribbon"].signals(_bars(c))
    assert (sig > 0).sum() == 1 and (sig < 0).sum() == 0


def test_a_breakout_against_supertrend_is_dropped(monkeypatch):
    b = _bars(_walk())
    real = S.REGISTRY["breakout_confirmed"].signals(b)

    def forced(d):
        monkeypatch.setattr(S.F, "supertrend", lambda h, l, c, n=10, mult=3.0: np.full(len(c), d, np.int8))
        return S.REGISTRY["breakout_confirmed"].signals(b)
    up, down = forced(1), forced(-1)
    assert (up >= 0).all() and (down <= 0).all()                          # each side needs supertrend with it
    assert (up > 0).any() and (down < 0).any()
    assert ((real == 0) | (real == up) | (real == down)).all()            # the filter only ever removes breakouts


@pytest.mark.parametrize("sid", NEW)
def test_new_strategies_are_causal(sid, flat_btc):
    c = _walk(900, seed=7)
    v = 100 + 50 * np.abs(np.random.default_rng(3).normal(size=900))
    full = S.REGISTRY[sid].signals(_bars(c, v=v))
    cut = S.REGISTRY[sid].signals(_bars(c[:600], v=v[:600]))
    assert (full[:600] == cut).all()
