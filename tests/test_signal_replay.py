"""Signal replay (research, 19 Oct 2026): a bar that touches both the stop and the target counts as the stop, the
entry is the next bar's open, and expiry uses the signal's own window."""
import numpy as np
import pandas as pd

from scripts.signal_replay import Series1m, _horizon, _run


def _series(rows):
    n = len(rows)
    o, h, l, c = (np.array([r[k] for r in rows], float) for k in range(4))
    return Series1m(pd.DataFrame({"t": np.arange(n) * 60_000, "open": o, "high": h, "low": l, "close": c,
                                  "volume": np.ones(n), "tb": np.full(n, 0.5)}))


def test_a_bar_touching_both_counts_as_the_stop():
    s = _series([(100, 100, 100, 100)] * 3 + [(100, 102.5, 98.5, 102)] + [(102, 102, 102, 102)] * 5)
    assert _run(s, 3, 1, 100.0, 102.0, 99.0, 1.0, 2.0, 60)[0] == "lost"


def test_target_then_expiry():
    s = _series([(100, 100.5, 99.8, 100.2)] * 3 + [(100.2, 102.1, 100.1, 102)] + [(102, 102, 102, 102)] * 5)
    assert _run(s, 3, 1, 100.0, 102.0, 99.0, 1.0, 2.0, 60)[:2] == ("won", 102.0)
    flat = _series([(100, 100.1, 99.9, 100)] * 80)
    assert _run(flat, 3, 1, 100.0, 102.0, 99.0, 1.0, 2.0, 30)[0] == "expired"
    assert _run(flat, 60, 1, 100.0, 102.0, 99.0, 1.0, 2.0, 30)[0] == "running"     # not enough bars yet


def test_short_mirror_and_horizon_parsing():
    s = _series([(100, 100, 100, 100)] * 3 + [(100, 100.2, 97.9, 98)] + [(98, 98, 98, 98)] * 5)
    assert _run(s, 3, -1, 100.0, 98.0, 101.0, 1.0, 2.0, 60)[0] == "won"
    assert _horizon({"timeframe": "81m"}) == 81 and _horizon({"timeframe": "4h"}) == 240
    assert _horizon({"timeframe": "4h", "trade_mode": "swing"}) == 7 * 24 * 60
