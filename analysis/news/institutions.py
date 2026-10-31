"""Institution seed loader (W4.3)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "config" / "news_institutions.json"


@lru_cache(maxsize=1)
def load_institutions() -> list[dict]:
    return list(json.loads(_PATH.read_text(encoding="utf-8")).get("institutions") or [])
