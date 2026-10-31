"""P4 Mac scripts + Appendix A coverage (plan W15)."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestNewsP4(unittest.TestCase):
    def test_backfill_dry_run_logs_ledger(self):
        # a temp ledger: the real audit/trial_ledger.jsonl gained lines on every test run (31 Oct 2026)
        import os
        import tempfile
        led = Path(tempfile.mkdtemp()) / "trial_ledger.jsonl"
        before = 0
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "news_backfill.py"), "--dry-run"],
            cwd=ROOT, capture_output=True, text=True, env={**os.environ, "NEWS_LEDGER": str(led)},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(led.exists())
        self.assertGreater(led.stat().st_size, before)

    def test_appendix_a_collector_coverage(self):
        from collectors.news.registry import COLLECTOR_IDS, all_collectors

        self.assertEqual(len(all_collectors()), len(COLLECTOR_IDS))
        fix = ROOT / "tests" / "fixtures" / "news"
        for cid in COLLECTOR_IDS:
            with self.subTest(collector=cid):
                self.assertTrue(any(fix.iterdir()), "fixtures/news must not be empty")
