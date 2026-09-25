"""
When not to open a trade: the scheduled releases that move every market.

A Fed rate decision (FOMC), US inflation (CPI) and US jobs (NFP) come out at
known minutes. Around them crypto moves hard in both directions within
seconds: in the first hour after an FOMC statement, BTC's average move
roughly doubles (0.66% to 1.25%, Yang & Wang 2026, 41 statements) with no
reliable direction. A stop sized for a normal hour gets taken out by the
spike, whichever way the market finally goes. Research on these releases
agrees on the use: stand aside or size down, not bet on the direction.

So this is a blackout, not a signal. No new paper position opens inside a
window around each release; open positions keep their stops.

Also a blackout after high-impact news that was not scheduled — a tariff
post, a war headline, a surprise rate move. The news scorer tags those, and
a confident one opens a window of its own from when it was published.

The dates below are the remaining 2026 releases, in UTC. The US clocks change
on 1 Nov, which is why the same 08:30 ET release is 12:30 UTC in October and
13:30 UTC after. A US government shutdown can move CPI and NFP dates, as it
did in 2025, so check the list against bls.gov when one is in the news.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Event:
    at: datetime          # naive UTC
    kind: str             # "fomc" | "cpi" | "nfp" | "pce" | "ppi" | "news"
    name: str


# Minutes before and after each kind during which no new trade opens.
WINDOWS: dict[str, tuple[int, int]] = {
    "fomc": (60, 90),     # statement at 14:00 ET, press conference 30 min later
    "cpi": (30, 60),
    "nfp": (30, 60),
    "pce": (15, 30),
    "ppi": (15, 30),
    "news": (0, 60),      # unscheduled: from publication
}

def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s)


EVENTS_2026: tuple[Event, ...] = (
    Event(_utc("2026-10-02T12:30"), "nfp", "US jobs report (Sep)"),
    Event(_utc("2026-10-14T12:30"), "cpi", "US CPI (Sep)"),
    Event(_utc("2026-10-15T12:30"), "ppi", "US PPI (Sep)"),
    Event(_utc("2026-10-28T18:00"), "fomc", "FOMC rate decision"),
    Event(_utc("2026-10-29T12:30"), "pce", "US PCE + GDP Q3 advance"),
    Event(_utc("2026-11-06T13:30"), "nfp", "US jobs report (Oct)"),
    Event(_utc("2026-11-10T13:30"), "cpi", "US CPI (Oct)"),
    Event(_utc("2026-11-13T13:30"), "ppi", "US PPI (Oct)"),
    Event(_utc("2026-11-25T13:30"), "pce", "US PCE (Oct)"),
    Event(_utc("2026-12-04T13:30"), "nfp", "US jobs report (Nov)"),
    Event(_utc("2026-12-09T19:00"), "fomc", "FOMC rate decision + projections"),
    Event(_utc("2026-12-10T13:30"), "cpi", "US CPI (Nov)"),
    Event(_utc("2026-12-15T13:30"), "ppi", "US PPI (Nov)"),
    Event(_utc("2026-12-23T13:30"), "pce", "US PCE (Nov)"),
)

HIGH_IMPACT_NEWS_CONFIDENCE = 0.7


def active_blackout(now: datetime, extra: tuple[Event, ...] = (),
                    events: tuple[Event, ...] = EVENTS_2026) -> Event | None:
    """The event whose window `now` (naive UTC) falls inside, or None."""
    for ev in (*events, *extra):
        before, after = WINDOWS.get(ev.kind, (0, 0))
        if ev.at - timedelta(minutes=before) <= now <= ev.at + timedelta(minutes=after):
            return ev
    return None


def news_events(rows, now: datetime, high_impact: set[str]) -> tuple[Event, ...]:
    """High-impact, confidently scored headlines from the last hour, as events."""
    before, after = WINDOWS["news"]
    out = []
    for r in rows:
        if (r.event_type in high_impact and r.published_at is not None
                and (r.confidence or 0) >= HIGH_IMPACT_NEWS_CONFIDENCE
                and now - timedelta(minutes=after) <= r.published_at <= now):
            out.append(Event(r.published_at, "news", f"{r.event_type}: {r.headline[:80]}"))
    return tuple(out)


def upcoming(now: datetime, days: int = 14,
             events: tuple[Event, ...] = EVENTS_2026) -> list[Event]:
    """Scheduled releases in the next `days`, soonest first, for the dashboard."""
    horizon = now + timedelta(days=days)
    return sorted((e for e in events if now <= e.at <= horizon), key=lambda e: e.at)
