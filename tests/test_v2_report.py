"""
The variant registries run_backtest() reads (analysis/v2_report.py): every
entry must actually be a valid V2Config/ExecConfig override, and TRIALS
must count every variant that goes into the same statistical test — a typo
here would either crash the real backtest mid-run (data.binance.vision is
unreachable from this sandbox, so that failure mode is invisible in dev
without a check like this) or silently under-count trials and understate
how much the multiple-comparisons penalty should be.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from analysis.v2_backtest import ExecConfig
from analysis.v2_report import SETUP_VARIANTS, TRIALS, VARIANT_LABELS, VARIANTS
from analysis.v2_setups import V2Config


class TestVariantRegistries(unittest.TestCase):
    def test_every_execution_variant_is_a_real_execconfig(self):
        for name, ex in VARIANTS.items():
            with self.subTest(name=name):
                self.assertIsInstance(ex, ExecConfig)

    def test_every_execution_variant_except_base_has_a_label(self):
        for name in VARIANTS:
            with self.subTest(name=name):
                self.assertIn(name, VARIANT_LABELS)

    def test_every_setup_variant_applies_cleanly_to_v2config(self):
        """dataclasses.replace() raises on an unknown field — the exact
        crash the base default caught (see test_v2.py's regression test)."""
        for name, (flags, label) in SETUP_VARIANTS.items():
            with self.subTest(name=name):
                cfg = replace(V2Config(), **flags)
                self.assertIsInstance(cfg, V2Config)
                self.assertTrue(label)

    def test_trials_counts_every_variant(self):
        self.assertEqual(TRIALS, len(VARIANTS) + len(SETUP_VARIANTS))

    def test_the_wider_d_stop_variant_only_widens_d(self):
        """The 27 Sep finding: D's stop buffer was hardcoded at 0.1, tighter
        than every other setup's, and the prime suspect for D's worst-of-
        the-four live shadow numbers. This variant is how the next real
        backtest checks that against actual data instead of a guess."""
        flags, _ = SETUP_VARIANTS["wider_d_stop"]
        self.assertEqual(flags, {"stop_atr_buffer_d": 0.25})
        cfg = replace(V2Config(), **flags)
        self.assertEqual(cfg.stop_atr_buffer, V2Config().stop_atr_buffer)   # A untouched


if __name__ == "__main__":
    unittest.main()
