"""Load W4.1 country metadata."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "config" / "countries.json"


@lru_cache(maxsize=1)
def load_countries() -> list[dict]:
    return list(json.loads(_PATH.read_text(encoding="utf-8")).get("countries") or [])


def country_iso2(name_or_iso: str) -> str:
    q = (name_or_iso or "").strip().upper()
    for c in load_countries():
        if q in (c.get("iso2", "").upper(), c.get("name", "").upper()):
            return c["iso2"]
    return q[:2] if len(q) == 2 else "XX"
