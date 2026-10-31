"""Plan hard rule #3: news engine modules must not import execution."""
from __future__ import annotations

import ast
from pathlib import Path

ROOTS = (
    Path("analysis/news"),
    Path("collectors/news"),
    Path("storage/news_models.py"),
)


def _imports_execution(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("execution"):
            bad.append(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("execution"):
                    bad.append(alias.name)
    return bad


def test_news_packages_never_import_execution():
    offenders = []
    for root in ROOTS:
        paths = [root] if root.suffix == ".py" else root.rglob("*.py")
        for p in paths:
            if not p.is_file() or p.name.startswith("__"):
                continue
            bad = _imports_execution(p)
            if bad:
                offenders.append((str(p), bad))
    assert not offenders, offenders
