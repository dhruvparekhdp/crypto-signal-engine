"""
The event monitor, shadow mode: levels, calendar, timing, and the four books.
"""
import random
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis.event_calendar import calendar_context, calendar_text
from analysis.event_monitor import (
    ActiveEvent,
    active_window,
    confirmed_level,
    max_abs_move,
    monitor_model_role,
    next_delay_minutes,
    parse_monitor,
    simulate_books,
)

T0 = datetime(2026, 10, 28, 18, 0)


def path(prices, start=T0, step_min=2):
    return [(start + timedelta(minutes=i * step_min), p) for i, p in enumerate(prices)]


class TestCalendar(unittest.TestCase):
    def names(self, when):
        return [c.name for c in calendar_context(datetime.fromisoformat(when))]

    def test_quarterly_expiry_is_flagged_in_its_week_only(self):
        self.assertTrue(any("Quarterly" in n for n in self.names("2026-09-25 07:00")))
        self.assertFalse(any("expiry" in n and "Quarterly" in n
                             for n in self.names("2026-09-10 07:00")))

    def test_fomc_day_leads_with_the_decision(self):
        items = calendar_context(datetime.fromisoformat("2026-10-28 17:30"))
        self.assertEqual(items[0].name, "FOMC rate decision")
        self.assertGreaterEqual(items[0].level, 4)

    def test_weekends_say_liquidity_is_thin(self):
        self.assertTrue(any("Weekend" in n for n in self.names("2026-09-27 11:00")))

    def test_fiscal_year_turns_are_known(self):
        self.assertTrue(any("fiscal year" in n for n in self.names("2026-09-28 09:00")))
        self.assertTrue(any("India and Japan" in n for n in self.names("2027-03-29 09:00")))

    def test_the_prompt_header_names_the_time(self):
        self.assertIn("Wednesday 28 October 2026", calendar_text(T0))


class TestLevels(unittest.TestCase):
    def test_the_market_confirms_a_level_from_btc_move(self):
        self.assertEqual([confirmed_level(x) for x in (0.2, 0.6, 1.5, 2.5, 5.0)],
                         [1, 2, 3, 4, 5])

    def test_max_move_is_measured_from_the_event_price(self):
        pts = path([100, 101, 97, 99])
        self.assertAlmostEqual(max_abs_move(pts, T0), 3.0)

    def test_memory_is_longer_for_bigger_events(self):
        now = T0 + timedelta(days=2)
        evs = [ActiveEvent(1, "war", "geopolitics_war", 5, T0),
               ActiveEvent(2, "speech", "central_bank", 2, T0)]
        self.assertEqual([e.id for e in active_window(evs, now)], [1])

    def test_parse_drops_unknown_ids_and_maps_bad_categories(self):
        got = parse_monitor({"new": [{"title": "X", "category": "made_up", "level": 9}],
                             "updates": [{"id": 5, "level": 4}, {"id": 99}],
                             "resolved": [5, 77]}, {5})
        self.assertEqual(got["new"][0]["category"], "other")
        self.assertEqual(got["new"][0]["level"], 5)
        self.assertEqual([u["id"] for u in got["updates"]], [5])
        self.assertEqual(got["resolved"], [5])


class TestTiming(unittest.TestCase):
    def test_bigger_events_mean_sooner_checks_with_jitter(self):
        rng = random.Random(1)
        calm = [next_delay_minutes(0, rng) for _ in range(200)]
        shock = [next_delay_minutes(5, rng) for _ in range(200)]
        self.assertTrue(all(27 <= x <= 63 for x in calm))
        self.assertTrue(all(3 <= x <= 7 for x in shock))
        self.assertGreater(len(set(calm)), 50)

    def test_calm_uses_the_fast_model(self):
        self.assertEqual(monitor_model_role(1), "briefing_calm")
        self.assertEqual(monitor_model_role(4), "briefing")


class TestShadowBooks(unittest.TestCase):
    def books(self, prices, level=4):
        paths = {s: path(prices) for s in ("btcusdt", "ethusdt", "solusdt")}
        return {b.book: b for b in simulate_books(T0, level, paths)}

    def test_a_clean_trend_after_the_spike_pays_the_breakout(self):
        # Spike both ways for 15 minutes, then a steady climb.
        prices = [100, 100.6, 99.5, 100.4, 99.6, 100.3, 99.8, 100.2] + \
                 [100.3 + 0.15 * i for i in range(150)]
        b = self.books(prices)
        self.assertEqual(b["C_confirmed_breakout"].side, "long")
        self.assertGreater(b["C_confirmed_breakout"].wallet_pct, 0)
        self.assertGreater(b["D_basket"].wallet_pct, 0)
        self.assertEqual(b["A_pause"].wallet_pct, 0)

    def test_a_whipsaw_stops_the_release_minute_entry(self):
        # Up first, then straight down: B goes long on the first move and is stopped.
        prices = [100, 100.8, 100.2, 99.6, 99.0, 98.4] + [98.4] * 60
        b = self.books(prices)
        self.assertEqual(b["B_double_at_release"].side, "long")
        self.assertLess(b["B_double_at_release"].wallet_pct, 0)

    def test_no_breakout_means_no_trade(self):
        b = self.books([100, 100.2, 99.9, 100.1] * 40)
        self.assertIn("no breakout", b["C_confirmed_breakout"].note)

    def test_thin_data_is_reported_not_guessed(self):
        got = simulate_books(T0, 4, {"btcusdt": path([100])})
        self.assertTrue(all(r.wallet_pct == 0 for r in got))


class TestWiring(unittest.TestCase):
    SRC = Path(__file__).resolve().parent.parent.joinpath("scheduler", "runner.py").read_text()

    def test_the_monitor_books_its_own_next_run(self):
        self.assertIn("self._schedule_monitor(delay)", self.SRC)
        self.assertIn("event_monitor_daily_cap", self.SRC)

    def test_a_fast_btc_move_triggers_a_check(self):
        self.assertIn("self._maybe_trigger_monitor(states)", self.SRC)

    def test_shadow_mode_never_touches_paper_trades(self):
        body = self.SRC[self.SRC.index("async def _event_evaluation_job"):
                        self.SRC.index("async def _market_briefing_job")]
        self.assertNotIn("open_position", body)
        self.assertNotIn("close_position", body)


if __name__ == "__main__":
    unittest.main()
