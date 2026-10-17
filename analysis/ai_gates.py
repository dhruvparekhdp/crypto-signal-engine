"""
When an AI job is worth running (owner, 18 Oct 2026: "it feels like we are wasting").

23 days of stored briefings: 75-95% said "nothing new", up to 127 a day. Each job now runs only when something
happened - new headlines from our feeds, a scheduled release close by, a sharp BTC move, or a big coin move - and
never on a bare timer. Pure functions so each rule is tested on its own.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def fresh_news_count(rows, since: datetime | None, now: datetime) -> int:
    """Headlines from our feeds published after `since` (not noise, not AI briefing rows, not future-dated)."""
    lo = since or (now - timedelta(hours=3))
    n = 0
    for r in rows:
        pub = getattr(r, "published_at", None)
        if pub is None or getattr(r, "event_type", "") == "noise":
            continue
        if (getattr(r, "source", "") or "").startswith("web_briefing"):
            continue
        if lo < pub <= now + timedelta(minutes=15):
            n += 1
    return n


def event_near(now: datetime, hours: float = 2.0, events=None) -> bool:
    """A scheduled release (CPI, FOMC, jobs...) within +-hours of now."""
    if events is None:
        from analysis.event_calendar import EVENTS_2026
        events = EVENTS_2026
    span = timedelta(hours=hours)
    return any(abs(e.at - now) <= span for e in events)


def btc_shock(closes_1m: list[float], pct: float = 2.0, minutes: int = 60) -> bool:
    """BTC moved at least pct% over the last `minutes` 1-minute closes."""
    if len(closes_1m) < 2:
        return False
    ref = closes_1m[-minutes - 1] if len(closes_1m) > minutes else closes_1m[0]
    return ref > 0 and abs(closes_1m[-1] / ref - 1) * 100 >= pct


def big_movers(moves, pct: float = 3.0) -> list:
    """Coins that moved at least pct% over the last 4 hours (the only ones worth explaining)."""
    return [m for m in moves if m.change_4h is not None and abs(m.change_4h) >= pct]
