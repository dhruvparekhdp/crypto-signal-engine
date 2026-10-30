"""
Paper-only news strategies (plan W11) — never touches execution or live books.

Owner 30 Oct 2026: stop-and-reverse and Fed-reaction templates are measured on
the candidates book before any live consideration.
"""
from __future__ import annotations

from analysis import impulse as im


def stop_reverse_levels(imp: im.Impulse, side: int, *, stop_r: float = 2.0) -> tuple[float, float]:
    """Stop-and-reverse: enter opposite side after impulse stop distance."""
    stop, _ = im.levels(imp, side)
    dist = abs(imp.price - stop)
    target = imp.price + side * stop_r * dist
    return stop, target


def fed_reaction_side(event_type: str, severity: int) -> int:
    """+1 risk-on lean after dovish Fed headline severity, -1 after hawkish — paper label only."""
    if event_type != "central_bank" or severity < 3:
        return 0
    return 1  # placeholder: direction comes from event metadata in full pipeline
