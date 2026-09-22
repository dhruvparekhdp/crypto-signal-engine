"""
The gold signal that could not happen, and the three gates that now stop it.

On 22 Sep the engine published XAUUSDT LONG with entry $4,341.08, target
$6,227.68 and a 36-minute horizon — a 43.459% move on gold in half an hour.
Every stage had worked correctly on one bad number:

  1. CoinGecko had no mapping for `xau`, so it fell through to /search and
     took a dead token with the right ticker, quoting gold at $0.00004049.
  2. That price was written into state, which poisoned the forming candle:
     one bar with a true range of the entire price.
  3. ATR14 went from 1.20 to ~311, i.e. 7.24% of price on a 1-minute bar.
  4. The level policy had a floor on target distance and no ceiling, so it
     turned 7.24% into a 43.5% target at reward:risk 3.
  5. The Groq reviewer said "mathematically impossible and structurally
     unsound" and was overruled, because it may only move confidence by 0.04.

These tests use those exact numbers. Each one fails against the old code.
"""
import unittest

from analysis.crypto_state_store import price_is_plausible
from analysis.scalp_levels import NoTrade, ScalpConfig, scalp_levels

GOLD_REAL = 4341.08
GOLD_JUNK = 4.049e-05       # what the dead XAU token was quoting
GOLD_ATR_CLEAN = 1.2028571  # measured off PAXGUSDT bars, from /api/debug


class TestPriceGate(unittest.TestCase):
    def test_the_gold_tick_that_started_it_is_refused(self):
        self.assertFalse(price_is_plausible("xauusdt", GOLD_REAL, GOLD_JUNK))

    def test_the_recovery_tick_is_refused_too(self):
        """Once the junk price is in, the true price looks like the outlier."""
        self.assertFalse(price_is_plausible("xauusdt", GOLD_JUNK, GOLD_REAL))

    def test_ordinary_moves_still_pass(self):
        for pct in (0.0, 0.001, 0.02, 0.15, 0.55):
            with self.subTest(move=pct):
                self.assertTrue(
                    price_is_plausible("btcusdt", 85000.0, 85000.0 * (1 + pct)))

    def test_a_violent_but_real_move_still_passes(self):
        """A 55% crash is a market event. The gate is for feed faults."""
        self.assertTrue(price_is_plausible("btcusdt", 85000.0, 85000.0 * 0.45))

    def test_zero_and_negative_are_never_prices(self):
        self.assertFalse(price_is_plausible("btcusdt", 85000.0, 0.0))
        self.assertFalse(price_is_plausible("btcusdt", 85000.0, -1.0))

    def test_a_cold_start_accepts_anything(self):
        """With no running price there is nothing to compare against."""
        self.assertTrue(price_is_plausible("btcusdt", 0.0, 85000.0))


class TestTargetCeiling(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalpConfig()

    def test_the_corrupt_atr_no_longer_produces_a_published_target(self):
        """7.24% one-minute ATR is the corrupted reading. Refuse, don't scale."""
        result = scalp_levels(
            entry=GOLD_REAL, is_long=True, atr_pct=0.07243, cfg=self.cfg,
            atr_target_multiple=1.5, symbol="xauusdt")
        self.assertIs(result, NoTrade.TARGET_ABSURD)

    def test_the_clean_atr_is_not_caught_by_this_gate(self):
        """
        The real reading must not trip the ceiling. It is still refused, as
        TOO_SLOW: at 0.028% a minute gold cannot cross a cost-derived target
        inside the hold. That is the cost model answering honestly, and it is
        a different answer from "the input is corrupt" — which is the point of
        giving the two cases separate reasons.
        """
        atr_pct = GOLD_ATR_CLEAN / GOLD_REAL          # ~0.0277%
        result = scalp_levels(
            entry=GOLD_REAL, is_long=True, atr_pct=atr_pct, cfg=self.cfg,
            atr_target_multiple=1.5, symbol="xauusdt")
        self.assertIsNot(result, NoTrade.TARGET_ABSURD)
        self.assertIs(result, NoTrade.TOO_SLOW)

    def test_a_normal_crypto_setup_is_unaffected(self):
        result = scalp_levels(
            entry=85381.36, is_long=False, atr_pct=0.0006, cfg=self.cfg,
            atr_target_multiple=1.5, symbol="btcusdt")
        self.assertNotIsInstance(result, NoTrade)

    def test_the_cap_is_the_boundary(self):
        """Just under passes, well over is refused — the gate is on distance."""
        cfg = self.cfg
        under = scalp_levels(entry=1000.0, is_long=True, atr_pct=0.02, cfg=cfg,
                             reward_risk=1.0, atr_target_multiple=1.0,
                             symbol="btcusdt")
        over = scalp_levels(entry=1000.0, is_long=True, atr_pct=0.20, cfg=cfg,
                            reward_risk=1.0, atr_target_multiple=1.0,
                            symbol="btcusdt")
        self.assertIs(over, NoTrade.TARGET_ABSURD)
        self.assertNotEqual(under, NoTrade.TARGET_ABSURD)


if __name__ == "__main__":
    unittest.main()
