"""The phone dashboard must send JSON a browser accepts."""
import json
import unittest

from scripts import dash


class TestDashJson(unittest.TestCase):
    def test_infinity_and_nan_become_null(self):
        data = {"pf": float("inf"), "nested": [{"x": float("nan"), "ok": 1.5}], "neg": float("-inf")}
        clean = dash.scrub(data)
        text = json.dumps(clean, allow_nan=False)        # raises if any non-finite number is left
        self.assertEqual(json.loads(text), {"pf": None, "nested": [{"x": None, "ok": 1.5}], "neg": None})

    def test_state_is_strict_json(self):
        from pathlib import Path
        if not Path("/proc/stat").exists():
            self.skipTest("dashboard server reads /proc (Linux)")
        json.dumps(dash.scrub(dash.state()), allow_nan=False)

    def test_page_script_parses(self):
        import re
        import shutil
        import subprocess
        import tempfile
        node = shutil.which("node")
        if not node:
            self.skipTest("node missing")
        from pathlib import Path
        js = re.search(r"<script>(.*?)</script>", Path("scripts/dash.html").read_text(), re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
        self.assertEqual(subprocess.run([node, "--check", f.name], capture_output=True, text=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()


class TestDashRobustness(unittest.TestCase):
    def test_a_stray_json_file_in_status_does_not_break_the_page(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "status").mkdir()
            (Path(d) / "status" / "banner.json").write_text(json.dumps({"title": "x", "items": []}))
            (Path(d) / "status" / "junk.json").write_text("{not json")
            (Path(d) / "status" / "job.json").write_text(json.dumps({"name": "j", "cmd": "c", "state": "done", "started": 1, "finished": 2, "beat": 2}))
            old = dash.ROOT
            dash.ROOT = Path(d)
            try:
                jobs = dash.load_jobs()
                self.assertEqual([j["name"] for j in jobs], ["j"])
                self.assertEqual(dash.banner_view()["title"], "x")
            finally:
                dash.ROOT = old


class TestBacktestProgress(unittest.TestCase):
    LOG = ["lab: 66 strategies x 12 symbols, exec=1m, cost=india_gst",
           "  BTCUSDT done (300s)", "  ETHUSDT done (300s)", "  SOLUSDT done (600s)"]

    def test_a_running_grid_reports_coins_done_of_total_and_an_eta(self):
        from scripts import dash
        p = dash.run_progress(self.LOG, "running", 650)
        self.assertEqual((p["done"], p["total"], p["strategies"]), (3, 12, 66))
        self.assertEqual(p["pct"], 25.0)
        self.assertAlmostEqual(p["eta_s"], 600 / 3 * 12 - 650)
        self.assertEqual([c["symbol"] for c in p["coins"]], ["BTC", "ETH", "SOL"])

    def test_a_finished_run_is_100_percent_with_its_trade_count(self):
        from scripts import dash
        p = dash.run_progress(self.LOG + ["1,119,177 trades in 1196s -> data/lab/runs/x"], "done", 1196)
        self.assertEqual((p["pct"], p["trades"]), (100.0, 1119177))
        self.assertNotIn("eta_s", p)

    def test_overall_counts_a_running_job_by_its_own_progress(self):
        from scripts import dash
        jobs = [{"state": "done", "progress_pct": 100, "progress": {"trades": 10}},
                {"state": "running", "progress_pct": 50, "progress": {"eta_s": 120}}]
        o = dash.overall(jobs)
        self.assertEqual((o["pct"], o["eta_s"], o["trades"], o["running"]), (75.0, 120, 10, 1))

    def test_jobs_get_plain_titles(self):
        from scripts import dash
        self.assertEqual(dash._describe("x", "python -m scripts.run_lab --tf 8h --null-trials 30")[1],
                         "8h null test: do the strategies beat random entries?")
        self.assertEqual(dash._describe("x", "python -m pytest -q")[0], "tests")
