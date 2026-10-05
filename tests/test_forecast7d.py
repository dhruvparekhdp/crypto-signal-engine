"""7-day forecast: nothing a forecast uses may come from after its date."""
import unittest

import numpy as np

from analysis import forecast7d as F7
from analysis.lab.data import Bars

DAY = 86_400_000


def bars(n=400, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    t = np.arange(n, dtype=np.int64) * DAY
    return Bars("TESTUSDT", "1d", t, c, c * 1.02, c * 0.98, c, np.ones(n), np.ones(n))


class TestNoLookAhead(unittest.TestCase):
    def test_volatility_at_a_bar_ignores_later_bars(self):
        b = bars()
        full = F7.daily_sigma(b.c)
        cut = F7.daily_sigma(b.c[:201])
        self.assertAlmostEqual(full[200], cut[200])

    def test_history_only_holds_outcomes_known_by_the_forecast_date(self):
        b = bars()
        h = F7.History()
        h.add_coin(b, F7.daily_sigma(b.c))
        t = int(b.t[250]) + DAY
        fz = h.before(t)
        # a forecast made at bar i has its outcome known when bar i+7 closes
        self.assertEqual(len(fz.y), sum(1 for k in h.t if k <= t))
        self.assertTrue(all(k <= t for k in np.array(h.t)[np.array(h.t) <= t]))
        self.assertLess(len(fz.y), len(h.y))

    def test_forecast_bands_are_ordered_and_widen_with_time(self):
        b = bars(800)
        s = F7.daily_sigma(b.c)
        h = F7.History()
        h.add_coin(b, s)
        fc = F7.forecast("TESTUSDT", b, s, 700, h.before(int(b.t[700]) + DAY))
        for d in fc.days:
            self.assertTrue(d["q10"] < d["q25"] < d["q50"] < d["q75"] < d["q90"])
        self.assertGreater(fc.days[-1]["q90"] - fc.days[-1]["q10"], fc.days[0]["q90"] - fc.days[0]["q10"])
        self.assertTrue(0 <= fc.p_up <= 1 and 0 <= fc.touch["+5%"] <= 1)
        self.assertEqual(fc.lean_z, 0.0)                 # lean is off unless asked for


if __name__ == "__main__":
    unittest.main()
