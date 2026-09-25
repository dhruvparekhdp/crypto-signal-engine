"""Mining the lake for past moves, and the facts a label may lean on."""

import unittest

import numpy as np
import pandas as pd

from analysis.history_review import (
    facts_before,
    notable_moves,
    outcome_after,
    parse_review,
    pick_timeframe,
    review_prompt,
    timeframe_costs,
)


def hourly(n=400, shock_at=300, shock=0.06, seed=1):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.003, n)
    ret[shock_at] = shock
    close = 100 * np.cumprod(1 + ret)
    open_ = np.r_[100, close[:-1]]
    ts = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({"ts": ts, "open": open_, "high": np.maximum(open_, close) * 1.001,
                         "low": np.minimum(open_, close) * 0.999, "close": close,
                         "volume": 100.0, "taker_buy_volume": 50.0})


class TestMoves(unittest.TestCase):
    def test_the_shock_is_found_and_the_noise_is_not(self):
        k = hourly()
        moves = notable_moves(k, z=4.0)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves.ts[0], k.ts[300])
        self.assertEqual(moves.direction[0], "up")
        self.assertGreater(moves.z[0], 4)

    def test_too_little_history_is_empty(self):
        self.assertTrue(notable_moves(hourly(n=50, shock_at=40)).empty)


class TestFacts(unittest.TestCase):
    def test_nothing_from_the_move_leaks_into_its_facts(self):
        k1h = hourly()
        at = k1h.ts[300]
        k15 = k1h.set_index("ts").resample("15min").ffill().reset_index()
        metrics = pd.DataFrame({"ts": pd.date_range(at - pd.Timedelta(hours=6), at, freq="5min"),
                                "sum_open_interest": np.linspace(100, 110, 73),
                                "sum_toptrader_long_short_ratio": 1.8,
                                "count_long_short_ratio": 2.1,
                                "sum_taker_long_short_vol_ratio": 0.9})
        f = facts_before(at, k15, k1h, metrics)
        self.assertAlmostEqual(f["price_before"], round(k1h.close[299], 6))
        # the last metrics row is AT the move and must not be used
        self.assertLess(f["oi_change_6h_pct"], 10.0)
        self.assertEqual(f["top_trader_ls"], 1.8)
        self.assertIn(f["structure_15m_12h"], ("up", "down", "range", "unclear"))
        after = outcome_after(at, k1h)
        self.assertGreater(after["move_1h_pct"], 5)

    def test_prompt_and_parse(self):
        text = review_prompt("solusdt", {"at": "2024-01-13 12:00", "ret_pct": 6.1, "z": 9.3,
                                         "facts": {"oi_change_6h_pct": 4.2}, "after": {}})
        self.assertIn("SOLUSDT moved +6.10%", text)
        self.assertIn("oi_change_6h_pct", text)
        got = parse_review({"move_type": "LIQUIDATION", "setup": "moonshot",
                            "visible_before": 1, "confidence": "nan",
                            "early_signs": ["funding 0.05%"] * 9})
        self.assertEqual(got["move_type"], "liquidation")
        self.assertEqual(got["setup"], "none")
        self.assertTrue(got["visible_before"])
        self.assertEqual(got["confidence"], 0.5)
        self.assertEqual(len(got["early_signs"]), 6)


class TestTimeframes(unittest.TestCase):
    def test_bigger_bars_cover_the_fees_more_times(self):
        rng = np.random.default_rng(3)
        n = 60 * 24 * 5
        close = 100 * np.cumprod(1 + rng.normal(0, 0.0006, n))
        k1m = pd.DataFrame({"ts": pd.date_range("2024-01-01", periods=n, freq="1min"),
                            "high": close * 1.0004, "low": close * 0.9996, "close": close})
        costs = timeframe_costs(k1m, 0.118)
        mult = [c["cost_multiple"] for c in costs]
        self.assertEqual(mult, sorted(mult))
        self.assertLess(costs[0]["cost_multiple"], 3)
        tf = pick_timeframe(costs, 3.0)
        self.assertIsNotNone(tf)
        self.assertNotEqual(tf, "1min")
        self.assertIsNone(pick_timeframe(costs, 1e9))


if __name__ == "__main__":
    unittest.main()
