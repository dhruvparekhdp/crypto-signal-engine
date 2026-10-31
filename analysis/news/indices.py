"""
Sentiment indices (plan W9) — LABELS only, not trade inputs.

Country / world / crypto scores from recent event severities and directions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IndexPoint:
    scope: str
    scope_key: str
    score: float
    confidence: float


def world_index(events: list[dict]) -> IndexPoint:
    """events: severity, direction (+1/-1/0 optional)."""
    if not events:
        return IndexPoint("world", "world", 0.0, 0.0)
    num = 0.0
    w = 0.0
    for e in events:
        sev = float(e.get("severity") or 1)
        sign = float(e.get("direction_sign") or 0)
        num += sign * sev
        w += sev
    score = num / w if w else 0.0
    score = max(-1.0, min(1.0, score / 5.0))
    conf = min(1.0, len(events) / 20.0)
    return IndexPoint("world", "world", round(score, 4), round(conf, 4))


def country_index(country: str, events: list[dict]) -> IndexPoint:
    c = country.upper()
    subset = [e for e in events if c in (e.get("countries") or [])]
    w = world_index(subset)
    return IndexPoint("country", c, w.score, w.confidence)
