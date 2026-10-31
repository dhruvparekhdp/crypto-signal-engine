"""
Event types and severity floors (plan W5) — one JSON source of truth.

Owner (Oct 2026 world-news plan): macro, central-bank, fiscal, geopolitical,
regulation, crypto-native, and "other" must classify the same way in collectors,
the resolver, and paper strategy labels.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "news_event_types.json"

EVENT_TYPES: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def load_event_types() -> dict[str, dict]:
    global EVENT_TYPES
    raw = json.loads(_CONFIG.read_text(encoding="utf-8"))
    types = raw.get("types") or {}
    EVENT_TYPES = tuple(sorted(types.keys()))
    return types


def event_type_severity_floor(event_type: str) -> int:
    """Minimum severity for a type when the classifier is uncertain."""
    info = load_event_types().get((event_type or "other").lower(), {})
    return int(info.get("severity_floor", 1))
