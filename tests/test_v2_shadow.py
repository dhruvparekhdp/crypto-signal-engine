"""Live v2 shadow step: same setups and fill rules as the backtest."""

import unittest
from types import SimpleNamespace

import pandas as pd

from analysis.v2_setups import generate
from analysis.v2_shadow import step
from tests.test_v2 import market


class TestShadowStep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.k5, cls.k15, cls.k4h, cls.k1d = market(40)
        cls.cands = generate("x", cls.k5, cls.k15, cls.k4h, cls.k1d)

    def frames_until(self, t):
        return {"5m": self.k5[self.k5.ts + pd.Timedelta(minutes=5) <= t],
                "15m": self.k15[self.k15.ts + pd.Timedelta(minutes=15) <= t],
                "4h": self.k4h[self.k4h.ts + pd.Timedelta(hours=4) <= t],
                "1d": self.k1d[self.k1d.ts + pd.Timedelta(days=1) <= t]}

    def test_a_live_step_finds_the_backtest_candidate_then_resolves_it(self):
        c = self.cands[len(self.cands) // 2]
        new, updates = step("x", self.frames_until(c.ts), None, [], c.ts)
        self.assertEqual(updates, [])
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0].ts, new[0].setup, new[0].side), (c.ts, c.setup, c.side))
        row = SimpleNamespace(id=1, symbol="x", setup=c.setup, side=c.side,
                              decided_at=c.ts.to_pydatetime(), entry=c.entry, stop=c.stop,
                              target=c.target, status="pending")
        later = c.ts + pd.Timedelta(days=2)
        _, updates = step("x", self.frames_until(later), None, [row], later)
        self.assertEqual(len(updates), 1)
        self.assertIn(updates[0].fields["status"], ("closed", "cancelled"))
        if updates[0].fields["status"] == "closed":
            self.assertIn(updates[0].fields["reason"], ("stop", "target", "time"))

    def test_no_new_position_while_one_is_resting(self):
        c = self.cands[len(self.cands) // 2]
        row = SimpleNamespace(id=1, symbol="x", setup=c.setup, side=c.side,
                              decided_at=c.ts.to_pydatetime(), entry=c.entry, stop=c.stop,
                              target=c.target, status="pending")
        new, _ = step("x", self.frames_until(c.ts), None, [row], c.ts)
        self.assertEqual(new, [])

    def test_empty_frames_do_nothing(self):
        self.assertEqual(step("x", {"5m": pd.DataFrame()}, None, [], pd.Timestamp.now()),
                         ([], []))


if __name__ == "__main__":
    unittest.main()
