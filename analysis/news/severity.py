"""
Severity rules (plan W5): map signals to 1–5 before optional LLM check (severity >= 3).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from analysis.news.event_types import event_type_severity_floor

_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "news_severity_rules.json"


@lru_cache(maxsize=1)
def load_severity_rules() -> list[dict]:
    raw = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    return list(raw.get("rules") or [])


def classify_severity(title: str, event_type: str = "other", *, actual_deviation: float | None = None) -> int:
    """
    Rule-based severity 1–5. LLM verification is applied later for scores >= 3 only.
    """
    t = (title or "").lower()
    base = event_type_severity_floor(event_type)
    score = base
    for rule in load_severity_rules():
        pat = rule.get("match")
        if pat and re.search(pat, t, re.I):
            score = max(score, int(rule.get("severity", base)))
    if actual_deviation is not None and abs(actual_deviation) >= 2.0:
        score = max(score, 4)
    elif actual_deviation is not None and abs(actual_deviation) >= 1.0:
        score = max(score, 3)
    return min(5, max(1, score))
