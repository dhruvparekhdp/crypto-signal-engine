"""Scalping research rules (owner, 18 Oct 2026): fire on the pattern they describe, and never look ahead."""
import numpy as np
import pytest

from analysis.lab import strategies as S
from analysis.lab.data import INTERVAL_MS, Bars

NEW = ("wick_reclaim", "range_fade", "vwap_stretch", "flow_reclaim", "flow_divergence", "flow_burst")


def _bars(o, h, l, c, v=None, tb=None):
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    n = len(c)
    v = np.full(n, 100.0) if v is None else np.asarray(v, float)
    tb = v * 0.5 if tb is None else np.asarray(tb, float)
    return Bars("ETHUSDT", "5m", np.arange(n, dtype=np.int64) * INTERVAL_MS["5m"] + 1_700_000_000_000, o, h, l, c, v, tb)


def _walk(n=1200, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.001, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.001, n)))
    v = 100 + 40 * np.abs(rng.normal(size=n))
    tb = v * np.clip(rng.normal(0.5, 0.08, n), 0.05, 0.95)
    return _bars(o, h, l, c, v, tb)


def _flush(n=400, at=300, buyers_next=0.7):
    """Flat at 100, then a bar that wicks to 98 on 5x volume and closes 99.9, then a green bar."""
    o = np.full(n, 100.0); c = np.full(n, 100.0); h = np.full(n, 100.05); lo = np.full(n, 99.95)
    v = np.full(n, 100.0); tb = np.full(n, 50.0)
    o[at], h[at], lo[at], c[at], v[at], tb[at] = 100.0, 100.05, 98.0, 99.9, 500.0, 200.0
    o[at + 1], h[at + 1], lo[at + 1], c[at + 1], v[at + 1], tb[at + 1] = 99.9, 100.2, 99.85, 100.15, 200.0, 200 * buyers_next
    return _bars(o, h, lo, c, v, tb), at


def test_new_rules_and_their_mirrors_are_registered():
    for sid in NEW:
        assert sid in S.REGISTRY and f"mirror_{sid}" in S.REGISTRY and S.REGISTRY[sid].tf == "5m"


def test_a_flush_that_reclaims_is_a_long():
    b, at = _flush()
    s = S.REGISTRY["wick_reclaim"].signals(b)
    assert s[at] == 1 and (s != 0).sum() == 1


def test_the_absorbed_flush_needs_buyers_on_the_next_bar():
    b, at = _flush(buyers_next=0.7)
    assert S.REGISTRY["flow_reclaim"].signals(b)[at + 1] == 1
    b, at = _flush(buyers_next=0.3)                                 # sellers still in control: no trade
    assert not S.REGISTRY["flow_reclaim"].signals(b).any()


def test_range_fade_only_in_a_flat_market():
    n = 800
    c = np.full(n, 100.0)
    o, h, l = c.copy(), c + 0.1, c - 0.1
    l[700], c[700] = 99.7, 99.95                                    # pokes below the 24h low, closes back inside
    s = S.REGISTRY["range_fade"].signals(_bars(o, h, l, c))
    assert s[700] == 1
    trend = 100 * np.exp(np.linspace(0, 0.1, n))                    # +10% over the window: not flat
    l2 = trend - 0.1
    l2[700] = trend[700] * 0.99
    assert S.REGISTRY["range_fade"].signals(_bars(trend, trend + 0.1, l2, trend))[700] == 0


@pytest.mark.parametrize("sid", NEW)
def test_scalp_rules_are_causal(sid):
    b = _walk()
    full = S.REGISTRY[sid].signals(b)
    cut = Bars(b.symbol, b.interval, b.t[:900], b.o[:900], b.h[:900], b.l[:900], b.c[:900], b.v[:900], b.tb[:900])
    assert (full[:900] == S.REGISTRY[sid].signals(cut)).all()
