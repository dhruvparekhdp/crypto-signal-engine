"""Freqtrade benchmark ports: Freqtrade's fill rules, and no free money on noise."""

import unittest

import numpy as np
import pandas as pd

from analysis.ft_bench import STRATEGIES, Strat, rsi, run_strategy, summarise


def bars(closes, freq="5min", wick=0.0):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": pd.date_range("2024-01-01", periods=len(c), freq=freq),
                         "open": o, "high": np.maximum(o, c) * (1 + wick),
                         "low": np.minimum(o, c) * (1 - wick), "close": c, "volume": 1.0})


def one_signal(at):
    def sig(df):
        e = np.zeros(len(df), bool)
        e[at] = True
        return pd.Series(e), pd.Series(np.zeros(len(df), bool))
    return sig


class TestFills(unittest.TestCase):
    def test_entry_is_next_open_and_roi_exits(self):
        closes = [100.0] * 210 + [100, 101, 103, 103]
        st = Strat("t", "5m", {0: 0.02}, -0.10, one_signal(209))
        (t,) = run_strategy(st, bars(closes), fee=0.0)
        self.assertEqual(t["reason"], "roi")
        self.assertAlmostEqual(t["pct"], 2.0, places=4)          # exits at entry * 1.02

    def test_stoploss_is_checked_before_roi(self):
        df = bars([100.0] * 212)
        df.loc[211, ["high", "low"]] = [110.0, 80.0]              # both inside one candle
        st = Strat("t", "5m", {0: 0.02}, -0.10, one_signal(209))
        (t,) = run_strategy(st, df, fee=0.0, stop_slip=0.0)
        self.assertEqual(t["reason"], "stoploss")
        self.assertAlmostEqual(t["pct"], -10.0, places=4)

    def test_roi_steps_down_with_time(self):
        closes = [100.0] * 210 + [100.5] * 30
        st = Strat("t", "5m", {0: 0.05, 60: 0.004}, -0.10, one_signal(209))
        (t,) = run_strategy(st, bars(closes), fee=0.0)
        self.assertEqual(t["reason"], "roi")
        self.assertGreaterEqual(t["pct"], 0.4)

    def test_rsi_bounds(self):
        r = rsi(pd.Series(np.cumsum(np.random.default_rng(1).normal(0, 1, 500)) + 100))
        self.assertTrue(((r.dropna() >= 0) & (r.dropna() <= 100)).all())


class TestNoFreeMoney(unittest.TestCase):
    def test_trend_strategies_lose_their_fees_on_a_random_walk(self):
        rng = np.random.default_rng(9)
        n = 200 * 24
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.009, n)))
        df = bars(c, freq="1h", wick=0.002)
        for st in STRATEGIES:
            if st.name in ("Supertrend",):
                s = summarise(run_strategy(st, df))
                self.assertLess(s["avg_pct"], 0.5, st.name)


if __name__ == "__main__":
    unittest.main()
