"""Price-action primitives, and above all: no bar may see the future."""

import unittest

import numpy as np
import pandas as pd

from analysis.structure import (
    atr,
    candle_momentum,
    engulfing,
    inside_bar,
    is_sweep_high,
    is_sweep_low,
    pin_bar,
    prev_day_levels,
    session_vwap,
    sr_zones,
    structure_states,
    swings,
    volume_profile,
)


def frame(closes, start="2024-01-01", freq="15min", wick=0.002):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=len(closes), freq=freq),
        "open": opens, "close": closes,
        "high": np.maximum(opens, closes) * (1 + wick),
        "low": np.minimum(opens, closes) * (1 - wick),
        "volume": 100.0})


def zigzag(legs, step=1.0, start=100.0):
    """Piecewise-linear path: legs like [+5, -3, +5, -3] in points per bar."""
    out, p = [start], start
    for leg in legs:
        for _ in range(abs(leg)):
            p += step if leg > 0 else -step
            out.append(p)
    return out


UP = zigzag([6, -3, 6, -3, 6, -3, 6])
UP_THEN_BREAK = zigzag([6, -3, 6, -3, 6, -3, 6, -12])


class TestSwingsAndStructure(unittest.TestCase):
    def test_swings_are_confirmed_k_bars_later(self):
        sw = swings(frame(UP), k=2)
        self.assertTrue(sw)
        self.assertTrue(all(s.confirm == s.idx + 2 for s in sw))
        highs = [s.price for s in sw if s.kind == "high"]
        self.assertEqual(highs, sorted(highs))                  # higher highs

    def test_uptrend_then_change_of_character(self):
        st = structure_states(frame(UP_THEN_BREAK))
        events = [e for e in st.event if e]
        self.assertIn("bos_up", events)
        self.assertEqual(events[-1], "choch_down")
        self.assertEqual(st.state.iloc[-1], "down")
        self.assertFalse(np.isnan(st.last_hl.iloc[-10]))

    def test_no_lookahead_anywhere(self):
        rng = np.random.default_rng(7)
        closes = 100 * np.cumprod(1 + rng.normal(0, 0.004, 300))
        df = frame(closes)
        full = structure_states(df)
        for cut in (60, 150, 299):
            part = structure_states(df.iloc[:cut].copy())
            pd.testing.assert_frame_equal(part, full.iloc[:cut], check_dtype=False)
        vw_full = session_vwap(df)
        pd.testing.assert_series_equal(session_vwap(df.iloc[:120]), vw_full.iloc[:120])
        a = atr(df)
        pd.testing.assert_series_equal(atr(df.iloc[:100]), a.iloc[:100])

    def test_zones_cluster_repeated_levels(self):
        sw = swings(frame(zigzag([5, -5, 5, -5, 5, -5, 5])), k=2)
        zones = sr_zones(sw, tolerance=0.6)
        self.assertTrue(any(z.touches >= 3 for z in zones))
        self.assertEqual(sr_zones(sw, tolerance=0.6, upto=0), [])


class TestLevels(unittest.TestCase):
    def test_previous_day_and_vwap(self):
        df = frame(np.linspace(100, 110, 96 * 2), freq="15min")
        lv = prev_day_levels(df)
        self.assertTrue(np.isnan(lv.pdh.iloc[10]))
        day1 = df.iloc[:96]
        self.assertAlmostEqual(lv.pdh.iloc[100], day1.high.max())
        self.assertAlmostEqual(lv.pdc.iloc[100], day1.close.iloc[-1])
        vw = session_vwap(df)
        tp0 = (df.high.iloc[0] + df.low.iloc[0] + df.close.iloc[0]) / 3
        self.assertAlmostEqual(vw.iloc[0], tp0)
        self.assertAlmostEqual(vw.iloc[96], (df.high.iloc[96] + df.low.iloc[96]
                                             + df.close.iloc[96]) / 3)   # reset

    def test_volume_profile(self):
        df = frame([100] * 50 + list(np.linspace(100, 110, 10)), wick=0.001)
        poc, vah, val = volume_profile(df)
        self.assertLess(abs(poc - 100), 0.5)
        self.assertLessEqual(val, poc)
        self.assertGreaterEqual(vah, poc)


class TestTriggers(unittest.TestCase):
    def bars(self, rows):
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])

    def test_candles(self):
        df = self.bars([(101, 101.2, 99.8, 100, 1), (99.9, 101.6, 99.7, 101.5, 1)])
        self.assertEqual(engulfing(df, 1), "bull")
        df = self.bars([(100, 100.5, 97, 100.3, 1)])
        self.assertEqual(pin_bar(df, 0), "bull")
        df = self.bars([(100, 102, 98, 101, 1), (100.5, 101, 99, 100.8, 1)])
        self.assertTrue(inside_bar(df, 1))
        df = self.bars([(100, 101, 99.9, 101, 1)] * 3)
        self.assertGreater(candle_momentum(df, 2), 0.8)

    def test_sweeps(self):
        rows = [(100, 100.5, 99.5, 100, 10)] * 20 + [(100, 100.2, 98.7, 99.6, 30)]
        df = self.bars(rows)
        self.assertTrue(is_sweep_low(df, 20, level=99.0, atr_val=1.0))
        self.assertFalse(is_sweep_low(df, 20, level=99.0, atr_val=0.2))   # too deep
        self.assertFalse(is_sweep_low(df, 20, level=99.8, atr_val=1.0))   # closed below
        rows[-1] = (100, 101.3, 99.9, 100.4, 30)
        self.assertTrue(is_sweep_high(self.bars(rows), 20, level=101.0, atr_val=1.0))


if __name__ == "__main__":
    unittest.main()
