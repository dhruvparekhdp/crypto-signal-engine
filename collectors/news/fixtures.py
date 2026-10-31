"""Recorded responses for tests (plan W15 — no network in tests)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "news"


def fixture_path(name: str) -> Path:
    return _ROOT / name


def load_fixture(name: str) -> Any:
    return json.loads(fixture_path(name).read_text(encoding="utf-8"))


def load_fixture_text(name: str) -> str:
    return fixture_path(name).read_text(encoding="utf-8")


def use_fixtures() -> bool:
    return os.environ.get("NEWS_USE_FIXTURES", "").lower() in ("1", "true", "yes")
