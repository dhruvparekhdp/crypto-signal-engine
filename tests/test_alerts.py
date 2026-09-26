"""Real-time Telegram alerts: errors while running, and crashes on restart."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from scheduler import alerts


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        alerts.configure(Path(self.dir.name))
        alerts._last_sent.clear()
        alerts._queue = asyncio.Queue()

    def tearDown(self):
        alerts._queue = None
        self.dir.cleanup()

    def test_an_error_alerts_once_with_where_it_happened(self):
        try:
            {}["missing"]
        except KeyError:
            alerts.processor(None, "exception", {"event": "paper_trading_job_failed",
                                                 "exc_info": True})
            alerts.processor(None, "exception", {"event": "paper_trading_job_failed",
                                                 "exc_info": True})     # cooldown
        self.assertEqual(alerts._queue.qsize(), 1)
        msg = alerts._queue.get_nowait()
        self.assertIn("paper_trading_job_failed", msg)
        self.assertIn("KeyError", msg)
        self.assertIn("tests/test_alerts.py", msg)

    def test_info_logs_do_not_alert(self):
        alerts.processor(None, "info", {"event": "heartbeat"})
        self.assertEqual(alerts._queue.qsize(), 0)

    def test_crash_is_detected_and_a_clean_stop_is_not(self):
        self.assertIsNone(alerts.mark_running("abc"))          # first ever start
        alerts.mark_clean_stop()
        self.assertIsNone(alerts.mark_running("abc"))          # stopped cleanly
        prev = alerts.mark_running("abc")                      # previous run died
        self.assertIsNotNone(prev)
        report = alerts.crash_report(prev)
        self.assertIn("crashed", report)
        self.assertIn("out-of-memory", report)                 # no error recorded

    def test_the_crash_report_names_the_last_error(self):
        alerts.mark_running("abc")
        alerts.processor(None, "error", {"event": "klines_job_failed", "error": "timeout"})
        prev = alerts.mark_running("abc")
        report = alerts.crash_report(prev)
        self.assertIn("klines_job_failed", report)
        self.assertIn("timeout", report)

    def test_an_error_without_exception_shows_its_fields(self):
        alerts.processor(None, "error", {"event": "price_tick_rejected", "symbol": "solusdt",
                                         "current": 150.2, "incoming": 0.0021,
                                         "timestamp": "x"})
        msg = alerts._queue.get_nowait()
        self.assertIn("symbol=solusdt", msg)
        self.assertIn("incoming=0.0021", msg)
        self.assertNotIn("timestamp", msg)


class NoEventKeywordInLogCalls(unittest.TestCase):
    """structlog's first argument is named `event`: log.info("x", event=...) raises
    TypeError at run time (it broke the news-bias lookup on 26 Sep)."""

    def test_no_log_call_passes_event_as_a_keyword(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        call = re.compile(r"\blog\.(?:debug|info|warning|error|exception|critical)\(")
        bad = []
        for path in root.rglob("*.py"):
            if any(part in (".venv", "venv", "tests", "site-packages") for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in call.finditer(text):
                depth, i = 1, m.end()
                while i < len(text) and depth:
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    i += 1
                if re.search(r"[,(\s]event\s*=[^=]", text[m.end():i]):
                    bad.append(f"{path.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
