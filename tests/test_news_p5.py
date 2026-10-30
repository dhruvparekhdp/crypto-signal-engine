"""P5 paper strategy helpers (W11)."""
from __future__ import annotations

import unittest

from analysis import impulse as im
from analysis.candidate_book import CONTROL, EXIT_OF
from analysis.news.paper_strategies import fed_reaction_side, stop_reverse_levels


class TestNewsP5(unittest.TestCase):
    def test_stop_reverse_levels(self):
        imp = im.Impulse("BTCUSDT", -1, -2.0, 5.0, 4.0, 95000.0, 96000.0, 94000.0, 1_700_000_000)
        stop, target = stop_reverse_levels(imp, 1, stop_r=2.0)
        self.assertLess(stop, imp.price)
        self.assertGreater(target, imp.price)

    def test_fed_reaction_needs_central_bank(self):
        self.assertEqual(fed_reaction_side("other", 4), 0)
        self.assertEqual(fed_reaction_side("central_bank", 4), 1)

    def test_candidate_book_registers_w11_tracks(self):
        self.assertEqual(CONTROL["stop_reverse_2r"], "stop_same_side_2r")
        self.assertIn("fed_reaction", EXIT_OF)
