"""v2 setups and their backtest: honest fills, cost gates, no lookahead."""

import unittest

import numpy as np
import pandas as pd

from analysis.v2_backtest import ExecConfig, grade, monte_carlo, simulate, stats
from analysis.v2_setups import Candidate, V2Config, _passes_costs, generate

T0 = pd.Timestamp("2024-03-04 08:00")      # a Monday, in session


def bars(rows, start=T0, freq="5min"):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["volume"] = 100.0
    df.insert(0, "ts", pd.date_range(start, periods=len(df), freq=freq))
    return df


def cand(side="long", entry=100.0, stop=99.0, target=102.0, at=T0):
    return Candidate(at, "x", "A", side, entry, stop, target)


class TestExecution(unittest.TestCase):
    def test_limit_must_trade_through_or_no_trade(self):
        k5 = bars([(100.2, 100.5, 100.0, 100.3)] * 5)      # low touches 100, never below
        self.assertEqual(simulate([cand()], k5), [])

    def test_target_pays_maker_both_ways(self):
        k5 = bars([(100.2, 100.3, 99.9, 100.1), (100.1, 102.5, 100.0, 102.2)])
        (t,) = simulate([cand()], k5)
        ex = ExecConfig()
        self.assertEqual(t.reason, "target")
        costs = 2 * ex.maker + ex.funding_per_8h * (2 * 5 / 60) / 8
        self.assertAlmostEqual(t.r, (0.02 - costs) / 0.01, places=4)

    def test_same_bar_stop_and_target_is_a_stop(self):
        k5 = bars([(100.2, 100.3, 99.9, 100.1), (100.1, 102.5, 98.5, 101.0)])
        (t,) = simulate([cand()], k5)
        self.assertEqual(t.reason, "stop")
        self.assertLess(t.r, -1.0)                        # slippage and fees on top

    def test_time_stop(self):
        k5 = bars([(100.2, 100.3, 99.9, 100.1)] + [(100.1, 100.4, 99.95, 100.2)] * 30)
        (t,) = simulate([cand()], k5)
        self.assertEqual(t.reason, "time")
        self.assertEqual(t.bars, ExecConfig().time_stop_bars + 1)

    def test_one_position_per_symbol(self):
        k5 = bars([(100.2, 100.3, 99.9, 100.1)] + [(100.1, 100.4, 99.95, 100.2)] * 30)
        got = simulate([cand(), cand(at=T0 + pd.Timedelta(minutes=10))], k5)
        self.assertEqual(len(got), 1)

    def test_short_mirror(self):
        k5 = bars([(99.8, 100.1, 99.7, 99.9), (99.9, 100.0, 97.5, 97.8)])
        (t,) = simulate([cand("short", 100.0, 101.0, 98.0)], k5)
        self.assertEqual(t.reason, "target")
        self.assertGreater(t.r, 1.9)


class TestGates(unittest.TestCase):
    def test_costs_gate_is_direction_aware(self):
        cfg = V2Config()
        self.assertTrue(_passes_costs("long", 100, 99, 102, cfg))
        self.assertFalse(_passes_costs("long", 100, 99, 98, cfg))      # target below
        self.assertFalse(_passes_costs("short", 100, 99, 98, cfg))     # stop below
        self.assertFalse(_passes_costs("long", 100, 99.9, 100.3, cfg))  # stop inside fees
        self.assertFalse(_passes_costs("long", 100, 99, 101.2, cfg))   # 1.2R < 1.5R

    def test_grade_and_monte_carlo(self):
        rs = [2.0, -1.0, -1.0, 2.0, 1.5, -1.0] * 50
        s = stats(rs)
        self.assertEqual(s["trades"], 300)
        self.assertAlmostEqual(s["expectancy_r"], 0.417, places=3)
        mc = monte_carlo(rs, risk_pct=0.5)
        self.assertGreater(mc["final_equity_p50"], 1.0)
        self.assertEqual(grade([])["promote_to_paper"], False)


def market(n_days=40, seed=11):
    """Synthetic 1m path with trends and ranges, resampled to every timeframe."""
    rng = np.random.default_rng(seed)
    n = n_days * 1440
    drift = np.repeat(rng.normal(0, 0.00015, n // 720 + 1), 720)[:n]
    c = 100 * np.cumprod(1 + drift + rng.normal(0, 0.0008, n))
    o = np.r_[100, c[:-1]]
    k1 = pd.DataFrame({"ts": pd.date_range("2024-01-01", periods=n, freq="1min"), "open": o,
                       "high": np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0004, n))),
                       "low": np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0004, n))),
                       "close": c, "volume": rng.lognormal(3, 0.6, n)})

    def rs(f):
        return (k1.set_index("ts").resample(f).agg({"open": "first", "high": "max",
                                                    "low": "min", "close": "last",
                                                    "volume": "sum"}).dropna().reset_index())
    return rs("5min"), rs("15min"), rs("4h"), rs("1D")


class TestGenerate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k5, cls.k15, cls.k4h, cls.k1d = market()
        cls.cands = generate("x", cls.k5, cls.k15, cls.k4h, cls.k1d)

    def test_candidates_obey_every_hard_filter(self):
        self.assertTrue(self.cands, "synthetic market should produce some setups")
        cfg = V2Config()
        for c in self.cands:
            self.assertTrue(_passes_costs(c.side, c.entry, c.stop, c.target, cfg))
            self.assertLess(c.ts.weekday(), 5)
            self.assertTrue(7 <= c.ts.hour < 17)
            self.assertIn(c.setup, "ABCD")

    def test_no_lookahead(self):
        cut = self.cands[len(self.cands) // 2].ts
        k5 = self.k5[self.k5.ts + pd.Timedelta(minutes=5) <= cut]
        k15 = self.k15[self.k15.ts + pd.Timedelta(minutes=15) <= cut]
        k4h = self.k4h[self.k4h.ts + pd.Timedelta(hours=4) <= cut]
        k1d = self.k1d[self.k1d.ts + pd.Timedelta(days=1) <= cut]
        early = generate("x", k5, k15, k4h, k1d)
        want = [(c.ts, c.setup, c.side, round(c.entry, 8), round(c.stop, 8))
                for c in self.cands if c.ts <= cut]
        got = [(c.ts, c.setup, c.side, round(c.entry, 8), round(c.stop, 8)) for c in early]
        self.assertEqual(got, want)

    def test_backtest_runs_end_to_end(self):
        trades = simulate(self.cands, self.k5)
        self.assertTrue(trades)
        g = grade(trades)
        self.assertIn("gates", g)
        self.assertTrue(all(t.reason in ("target", "stop", "time") for t in trades))


if __name__ == "__main__":
    unittest.main()
