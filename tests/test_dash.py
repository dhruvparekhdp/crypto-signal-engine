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
