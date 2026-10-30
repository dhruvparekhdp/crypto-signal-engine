"""
Paper-only news strategies (plan W11) — never touches execution or live books.

Owner 30 Oct 2026: stop-and-reverse and Fed-reaction templates are measured on
the candidates book before any live consideration.
"""
from __future__ import annotations

import re

from analysis import impulse as im
from analysis.news.matchers import search_wb


def stop_reverse_levels(imp: im.Impulse, side: int, *, stop_r: float = 2.0) -> tuple[float, float]:
    """Stop-and-reverse: enter opposite side after impulse stop distance."""
    stop, _ = im.levels(imp, side)
    dist = abs(imp.price - stop)
    target = imp.price + side * stop_r * dist
    return stop, target


def fed_reaction_side(
    event_type: str,
    severity: int,
    *,
    direction: str = "",
    title: str = "",
    tone: str = "",
) -> int:
    """+1 after dovish Fed tone, -1 after hawkish — from event metadata, not a constant."""
    if event_type != "central_bank" or severity < 3:
        return 0
    text = f"{direction} {tone} {title}".lower()
    if search_wb(text, "hawkish|hike|tighten|restrictive|higher for longer"):
        return -1
    if search_wb(text, "dovish|cut|ease|accommodative|lower rates"):
        return 1
    if re.search(r"\bcut(s)?\s+rates\b", text):
        return 1
    return 0
