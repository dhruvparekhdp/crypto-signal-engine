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


def _helpers() -> str:
    """The shared time helpers: the parser, fmtStamp and the IST clock."""
    snippet = health._THEME_SNIPPET
    return snippet[snippet.index("var _toDate = function"):
                   snippet.index("window.fmtAgo = function")]


class TestTheFormatCarriesBoth(unittest.TestCase):
    def setUp(self):
        self.fn = _helpers()

    def test_it_asks_for_a_day_and_a_month(self):
        self.assertIn("day:'2-digit'", self.fn)
        # Month names from a fixed list: browsers disagree on "Sep"/"Sept".
        self.assertIn("'Jan','Feb','Mar'", self.fn)

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
        self.assertIn("Number(p.year) !== new Date().getFullYear()", self.fn)


class TestItSurvivesBadInput(unittest.TestCase):
    def setUp(self):
        self.fn = _helpers()

    def test_a_missing_stamp_renders_a_dash_rather_than_throwing(self):
        """
        The dashboard's old copy called iso.endsWith() with no guard, so one
        null opened_at took out the whole render.
        """
        self.assertIn("if(!iso) return null", self.fn)
        self.assertIn("return iso ? String(iso) : '—'", self.fn)

    def test_an_unparseable_stamp_is_shown_rather_than_swallowed(self):
        self.assertIn("return isNaN(d) ? null : d", self.fn)


class TestInTheBrowserEngine(unittest.TestCase):
    """Run the real helpers in Node: UTC in, IST out, including across midnight."""

    def run_js(self, body: str) -> list[str]:
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node not installed")
        snippet = health._THEME_SNIPPET
        js = snippet[snippet.index("if(!window.fmtStamp){"):
                     snippet.index("// Carry each table's column names")]
        src = ("var window=globalThis;var document={querySelectorAll:function(){return []}};"
               "var setInterval=function(){};" + js + body)
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().splitlines()

    def test_conversions(self):
        got = self.run_js(
            "console.log(fmtStamp('2026-09-26T08:35:00'));"
            "console.log(fmtStamp('2025-01-02 20:00:00'));"
            "console.log(fmtStamp('2026-09-25T23:10:00+05:30'));"
            "console.log(istClock('2026-09-26T08:35:00Z'));"
            "console.log(istHour(7) + ' ' + istHour(17));"
            "console.log(fmtStamp(null) + ' ' + fmtStamp('garbage'));")
        self.assertEqual(got[0][-9:], "14:05 IST")
        self.assertTrue(got[0].startswith("26 Sep"))
        self.assertEqual(got[1], "03 Jan 2025 01:30 IST")   # next day in IST
        self.assertTrue(got[2].endswith("23:10 IST"))
        self.assertEqual(got[3], "14:05 IST")
        self.assertEqual(got[4], "12:30 22:30")
        self.assertEqual(got[5], "— garbage")

    def test_relative(self):
        got = self.run_js(
            "var n=Date.now(),i=function(ms){return new Date(n+ms).toISOString()};"
            "console.log([-10e3,-5*60e3,-80*60e3,-26*3600e3,5*60e3,80*60e3]"
            ".map(function(ms){return fmtAgo(i(ms))}).join('|'));")
        self.assertEqual(got[0], "just now|5m ago|1h 20m ago|1d 2h ago|in 5m|in 1h 20m")


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
