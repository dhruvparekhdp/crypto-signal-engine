"""
One timestamp format, on every page, carrying both the date and the time.

There were two implementations of fmtTime and they had drifted. The audit page
printed "24 Sept 15:39"; the dashboard printed "03:39 pm IST" with no date at
all. That was survivable while trades lasted twenty minutes and everything on
screen was obviously from today. Widening the stop ended that: holds run for
hours now and the closed-trade history spans days, so "11:14 am" no longer
says which day, and two rows an hour apart on screen can be two days apart in
fact.

The zone handling is the part worth pinning hardest. Every timestamp on the
wire is UTC and most arrive with no suffix saying so, which `new Date(iso)`
reads as the viewer's LOCAL time — rendering every signal five and a half
hours early for an operator in India, and differently again for anyone else.
"""
import re
import unittest
from pathlib import Path

from scheduler import health

SRC = Path(__file__).resolve().parent.parent / "scheduler/health.py"


class TestThereIsOneImplementation(unittest.TestCase):
    def test_the_formatter_is_defined_once_in_the_shared_snippet(self):
        self.assertIn("window.fmtStamp = function", health._THEME_SNIPPET)
        self.assertEqual(health._THEME_SNIPPET.count("window.fmtStamp = function"), 1)

    def test_no_page_formats_a_time_by_itself(self):
        """
        Two copies is how the formats diverged. toLocaleTimeString and
        toLocaleDateString each render only half of what was asked for, so
        their presence anywhere is the bug returning.
        """
        source = SRC.read_text()
        for banned in ("toLocaleTimeString", "toLocaleDateString"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_every_page_can_reach_it(self):
        for name in ("_HTML", "_DATA_HTML", "_SETTINGS_HTML", "_AUDIT_HTML",
                     "_PREDICT_HTML"):
            with self.subTest(page=name):
                self.assertIn("window.fmtStamp = function", getattr(health, name))


class TestTheFormatCarriesBoth(unittest.TestCase):
    def setUp(self):
        snippet = health._THEME_SNIPPET
        self.fn = snippet[snippet.index("window.fmtStamp = function"):
                          snippet.index("window.fmtAgo = function")]

    def test_it_asks_for_a_day_and_a_month(self):
        self.assertIn("day:'2-digit'", self.fn)
        self.assertIn("month:'short'", self.fn)

    def test_it_asks_for_an_hour_and_a_minute(self):
        self.assertIn("hour:'2-digit'", self.fn)
        self.assertIn("minute:'2-digit'", self.fn)

    def test_it_renders_in_the_operators_zone_not_the_viewers(self):
        self.assertIn("timeZone:'Asia/Kolkata'", self.fn)

    def test_a_stamp_with_no_zone_is_read_as_utc(self):
        """
        The wire sends naive UTC. Read as local time it is 5h30m out for the
        operator, and out by a different amount for everyone else.
        """
        self.assertIn("+ 'Z'", self.fn)

    def test_the_year_appears_only_when_it_is_not_this_one(self):
        self.assertIn("getFullYear() !== now.getFullYear()", self.fn)


class TestItSurvivesBadInput(unittest.TestCase):
    def setUp(self):
        snippet = health._THEME_SNIPPET
        self.fn = snippet[snippet.index("window.fmtStamp = function"):
                          snippet.index("window.fmtAgo = function")]

    def test_a_missing_stamp_renders_a_dash_rather_than_throwing(self):
        """
        The dashboard's old copy called iso.endsWith() with no guard, so one
        null opened_at took out the whole render.
        """
        self.assertIn("if(!iso) return", self.fn)

    def test_an_unparseable_stamp_is_shown_rather_than_swallowed(self):
        self.assertIn("if(isNaN(d)) return String(iso)", self.fn)


class TestTheRelativeReadingIsAlsoAvailable(unittest.TestCase):
    """
    "3h ago" and "24 Sept 15:39" answer different questions — whether to care,
    and which row it was — so the signal cards show both.
    """

    def test_fmtago_exists_in_the_shared_snippet(self):
        self.assertIn("window.fmtAgo = function", health._THEME_SNIPPET)

    def test_the_signal_cards_show_the_stamp_and_the_relative_time(self):
        source = SRC.read_text()
        fn = source[source.index("function fmtSignalTime"):
                    source.index("function renderCryptoSignalCard")]
        self.assertIn("window.fmtStamp(iso)", fn)
        self.assertIn("window.fmtAgo(iso)", fn)


class TestTheRawDatabaseViewer(unittest.TestCase):
    def test_it_formats_timestamps_but_keeps_the_exact_value(self):
        """
        /data exists for checking what is actually stored, so the raw ISO has
        to stay reachable — it moves to the title rather than disappearing.
        """
        source = SRC.read_text()
        fn = source[source.index("function cell(v){"):
                    source.index("function renderTable(t){")]
        self.assertIn("window.fmtStamp(v", fn)
        self.assertIn('title="\'+esc(v)+\'"', fn)

    def test_the_pattern_matches_what_postgres_and_sqlite_actually_return(self):
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
        for stamp in ("2026-09-24T10:09:00.123456", "2026-09-24 10:09:00",
                      "2026-09-24T10:09:00+00:00"):
            with self.subTest(stamp=stamp):
                self.assertTrue(pattern.match(stamp))
        for other in ("btcusdt", "86713.6705", "confluence", ""):
            with self.subTest(other=other):
                self.assertFalse(pattern.match(other))
