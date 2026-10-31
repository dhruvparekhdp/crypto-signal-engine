"""P3 reactions, indices, portal API."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from analysis.news.indices import country_index, world_index
from analysis.news.reactions import reaction_move_pct


class TestNewsP3(unittest.TestCase):
    def test_reaction_move_pct(self):
        t0 = datetime(2026, 1, 1, 12, 0)
        bars = [(t0, 100.0), (t0 + timedelta(minutes=60), 101.0)]
        self.assertAlmostEqual(reaction_move_pct(bars, t0, 60), 1.0)

    def test_world_index_empty(self):
        w = world_index([])
        self.assertEqual(w.score, 0.0)
        self.assertEqual(w.confidence, 0.0)

    def test_country_index_filters(self):
        ev = [{"severity": 4, "direction_sign": -1, "countries": ["US"]}]
        c = country_index("US", ev)
        self.assertLess(c.score, 0)
