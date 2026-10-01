"""Every inline <script> in every served page must parse.

Regression: a raw-string page carried escaped backticks (\\`) in its JS, which is
a SyntaxError in the browser, so the whole dashboard sat on "Loading...".
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _pages():
    import importlib
    import pkgutil

    import scheduler

    for mod in pkgutil.iter_modules(scheduler.__path__):
        try:
            m = importlib.import_module(f"scheduler.{mod.name}")
        except Exception:
            continue
        for name, val in vars(m).items():
            if name.isupper() and isinstance(val, str) and "<script" in val and "</html>" in val.lower():
                yield f"{mod.name}.{name}", val


@unittest.skipUnless(NODE, "node is not installed")
class TestInlineJsParses(unittest.TestCase):
    def test_pages_were_discovered(self):
        self.assertGreaterEqual(len(list(_pages())), 4)

    def test_every_inline_script_parses(self):
        for label, html in _pages():
            for i, m in enumerate(SCRIPT.finditer(html)):
                body = m.group(1)
                if "{{" in body and "}}" in body and "$" not in body:
                    continue
                with self.subTest(page=label, script=i):
                    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                        f.write(body)
                    try:
                        r = subprocess.run([NODE, "--check", f.name], capture_output=True, text=True)
                    finally:
                        Path(f.name).unlink(missing_ok=True)
                    self.assertEqual(r.returncode, 0, r.stderr[:400])


if __name__ == "__main__":
    unittest.main()
