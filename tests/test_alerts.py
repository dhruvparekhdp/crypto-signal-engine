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


if __name__ == "__main__":
    unittest.main()
