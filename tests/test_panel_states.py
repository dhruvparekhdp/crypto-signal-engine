"""
A blank panel means three different things, and they all looked the same.

Still loading, loaded and genuinely empty, or the request died — the page
rendered the same nothing for each. /audit had five fetches and one catch
that wrote anything to the screen, so four of its failure paths left the
page sitting on whatever was there before. A dead endpoint and a quiet
market were indistinguishable, which is a bad way to lose an evening.

Panel makes the distinction structural: Panel.load() writes the loading
state, runs the work, and turns any failure into a message naming what broke
with a retry. The caller cannot forget the catch, because the catch is the
wrapper.
"""
import re
import unittest

import scheduler.health as health

PAGES = {
    "dashboard": health._HTML,
    "data": health._DATA_HTML,
    "settings": health._SETTINGS_HTML,
    "audit": health._AUDIT_HTML,
    "predict": health._PREDICT_HTML,
}


class TestPanelIsEverywhere(unittest.TestCase):
    def test_every_page_carries_the_helper(self):
        """It rides in the shared theme snippet, so no page can miss it."""
        for name, page in PAGES.items():
            with self.subTest(page=name):
                self.assertIn("window.Panel", page)

    def test_every_page_carries_its_styling(self):
        for name, page in PAGES.items():
            with self.subTest(page=name):
                self.assertIn(".pnl{padding", page)

    def test_the_three_states_are_visually_distinct(self):
        css = PAGES["audit"]
        for cls in (".pnl-err .pnl-t", ".pnl-load .pnl-t"):
            with self.subTest(rule=cls):
                self.assertIn(cls, css)


class TestFailuresReachTheContentNotJustAHeader(unittest.TestCase):
    """
    Each of these wrote its failure into a small status line while the body
    kept its previous content — or its "loading…" — forever.
    """

    def test_audit_puts_the_error_in_the_table(self):
        self.assertIn("Panel.error(document.getElementById('slice-body')",
                      PAGES["audit"])
        self.assertIn("Could not load the audit", PAGES["audit"])

    def test_predict_puts_the_error_in_the_rows(self):
        self.assertIn("Panel.error(document.getElementById('rows')", PAGES["predict"])

    def test_data_offers_a_retry_rather_than_a_dead_end(self):
        self.assertIn("Panel.error(document.getElementById('tables'), 'the tables', e, load)",
                      PAGES["data"])

    def test_a_non_200_is_treated_as_a_failure(self):
        """
        `fetch(...).then(r => r.json())` resolves happily on a 500 and then
        throws somewhere unrelated, or renders an error object as data.
        """
        for name in ("audit", "predict", "data"):
            with self.subTest(page=name):
                self.assertIn("returned ' + r", PAGES[name].replace("resp", "r"))


class TestEmptyStatesSayWhy(unittest.TestCase):
    def test_no_rows_explains_itself(self):
        self.assertIn("nothing has written to it yet", PAGES["data"])
        self.assertNotIn("— empty —", PAGES["data"])

    def test_an_empty_audit_window_tells_you_what_to_change(self):
        self.assertIn("Widen the day range", PAGES["audit"])

    def test_the_empty_helper_takes_a_reason(self):
        """"Nothing yet" and "nothing matching your filters" send you elsewhere."""
        m = re.search(r"function empty\(el, what, why\)", PAGES["audit"])
        self.assertIsNotNone(m, "empty() must accept a reason, not just a noun")


if __name__ == "__main__":
    unittest.main()
