"""Event reaction tracker (plan W10) — measures move after an event from bar series."""
from __future__ import annotations

from datetime import datetime, timedelta


def reaction_move_pct(closes: list[tuple[datetime, float]], event_at: datetime, horizon_min: int) -> float | None:
    """closes: (time, price) sorted. Return % change from last close before event to close at horizon."""
    if not closes:
        return None
    before = [p for t, p in closes if t <= event_at]
    if not before:
        return None
    p0 = before[-1]
    end = event_at + timedelta(minutes=horizon_min)
    after = [p for t, p in closes if event_at < t <= end]
    if not after:
        return None
    p1 = after[-1]
    if p0 <= 0:
        return None
    return (p1 - p0) / p0 * 100.0
