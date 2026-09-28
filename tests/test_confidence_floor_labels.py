"""
Part C (28 Sep review): the "signal shows 67-68% confidence, no trade
opened" confusion always traced back to two differently-labelled confidence
floors — crypto_min_confidence (signal generation) and the paper config's
own min_confidence (opening a trade) — in two different Settings sections.
These pin the label/help-text fix and the live cross-check warning.
"""
import unittest


class TestConfidenceFloorLabels(unittest.TestCase):
    def test_crypto_min_confidence_label_names_it_as_the_signal_floor(self):
        from config.overrides import FIELDS
        field = next(f for f in FIELDS if f.key == "crypto_min_confidence")
        self.assertIn("generate a signal", field.label.lower())
        self.assertIn("paper-trading floor", field.help)

    def test_crypto_min_confidence_is_still_in_the_signals_group(self):
        from config.overrides import FIELDS
        field = next(f for f in FIELDS if f.key == "crypto_min_confidence")
        self.assertEqual(field.group, "signals")

    def test_paper_min_confidence_label_names_it_as_the_trade_floor(self):
        import re

        from scheduler.settings_page import PAGE
        m = re.search(r"const PAPER_FIELDS=\[(.*?)\];", PAGE, re.S)
        self.assertIsNotNone(m, "PAPER_FIELDS array not found on the settings page")
        block = m.group(1)
        self.assertIn("min_confidence", block)
        self.assertIn("Minimum confidence to open a paper trade", block)
        self.assertIn("Minimum confidence to generate a signal at all", block)


class TestLiveCrossCheckWarning(unittest.TestCase):
    """The page must warn when the paper floor is set below the signal
    floor — that combination is always a dead configuration, since nothing
    under the signal floor ever becomes a signal for the paper floor to
    check in the first place."""

    def test_settings_page_declares_the_warning_section(self):
        from scheduler.settings_page import PAGE
        self.assertIn('id="confwarn"', PAGE)

    def test_settings_page_defines_the_cross_check_function(self):
        from scheduler.settings_page import PAGE
        self.assertIn("function checkConfidenceFloors()", PAGE)
        # Reads both floors by their DOM ids rather than only the
        # last-saved value, so an unsaved edit is caught immediately too.
        self.assertIn("f-crypto_min_confidence", PAGE)
        self.assertIn("pf-min_confidence", PAGE)

    def test_the_check_runs_after_load_and_on_every_edit(self):
        from scheduler.settings_page import PAGE
        # Wired into both the initial load and the two live-edit handlers,
        # so a change to either field re-evaluates it without a page reload.
        self.assertIn("renderPaper();\n  checkConfidenceFloors();", PAGE)
        self.assertRegex(PAGE, r"function mark\(k,v\)\{.*checkConfidenceFloors\(\);\}")
        self.assertRegex(PAGE, r"function markP\(k,v\)\{.*checkConfidenceFloors\(\);\}")


if __name__ == "__main__":
    unittest.main()
