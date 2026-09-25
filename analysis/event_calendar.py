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


# ── The recurring calendar ────────────────────────────────────────────────
#
# What kind of hour, day, week, month and quarter it is, before anyone asks
# a model anything. Every prompt the event monitor sends starts with this, so
# it asks about options expiry in expiry week, about the fiscal year when one
# turns over, and on a quiet weekend only about surprises. Levels are 1-5,
# the same scale the monitor uses for news.

@dataclass(frozen=True)
class CalendarItem:
    name: str
    level: int
    when: str          # human text, e.g. "today 08:00 UTC", "this week"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """The n-th given weekday of a month (weekday: Mon=0). n=-1 is the last."""
    if n > 0:
        d = datetime(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = datetime(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _last_business_day(year: int, month: int) -> datetime:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def calendar_context(now: datetime) -> list[CalendarItem]:
    """Recurring structure active or close at `now` (naive UTC), highest level first."""
    out: list[CalendarItem] = []
    d = now.date()
    wd = now.weekday()           # Mon=0 .. Sun=6
    y, m = now.year, now.month

    # Intraday
    for hh in (0, 8, 16):
        mins = (now.hour * 60 + now.minute) - hh * 60
        if -30 <= mins <= 15:
            out.append(CalendarItem("Binance perpetual funding settlement", 1, f"{hh:02d}:00 UTC"))
    if wd < 5:
        us_open = 13 if now.month in (11, 12, 1, 2) or (now.month == 3 and now.day < 9) else 12
        if us_open * 60 <= now.hour * 60 + now.minute + 30 <= us_open * 60 + 90:
            out.append(CalendarItem("US stock market open", 2, f"{us_open}:30 UTC"))
    # Weekly
    if wd == 4:
        out.append(CalendarItem("Deribit weekly options expiry", 2, "today 08:00 UTC"))
    if wd == 3:
        out.append(CalendarItem("US weekly jobless claims", 2, "today 12:30/13:30 UTC"))
    if wd >= 5 or (wd == 4 and now.hour >= 21):
        out.append(CalendarItem("Weekend: thin liquidity, CME Bitcoin futures closed "
                                "(gap risk at Sunday open)", 2, "until Sun 22:00 UTC"))
    # Monthly
    last_fri = _nth_weekday(y, m, 4, -1).date()
    if 0 <= (last_fri - d).days <= 4:
        lvl = 4 if m in (3, 6, 9, 12) else 3
        out.append(CalendarItem(("Quarterly" if lvl == 4 else "Monthly")
                                + " Deribit and CME Bitcoin options/futures expiry", lvl,
                                f"{last_fri:%a %d %b} 08:00 UTC"))
    lbd = _last_business_day(y, m).date()
    if 0 <= (lbd - d).days <= 2:
        out.append(CalendarItem("Month-end fund rebalancing", 2, f"through {lbd:%d %b}"))
    first_fri = _nth_weekday(y, m, 4, 1).date()
    if 0 <= (first_fri - d).days <= 2:
        out.append(CalendarItem("US jobs report (NFP), first Friday", 4, f"{first_fri:%a %d %b}"))
    # Quarterly
    if m in (3, 6, 9, 12):
        third_fri = _nth_weekday(y, m, 4, 3).date()
        if 0 <= (third_fri - d).days <= 5:
            out.append(CalendarItem("Triple witching: US stock index options and futures expiry",
                                    3, f"{third_fri:%a %d %b}"))
        if (lbd - d).days <= 5 and lbd >= d:
            out.append(CalendarItem("Quarter-end window dressing and rebalancing", 2,
                                    f"through {lbd:%d %b}"))
    if m in (1, 4, 7, 10) and d.day <= 31 and 10 <= d.day <= 31:
        out.append(CalendarItem("Earnings season: Coinbase, MicroStrategy, big tech, Nvidia",
                                2, "this month"))
    # Yearly
    yearly = [
        ((2, 1), "India Union Budget (crypto tax changes land here)", 3),
        ((4, 1), "India and Japan financial years start", 3),
        ((4, 15), "US tax day", 2),
        ((10, 1), "US federal fiscal year starts (shutdown risk if no funding bill)", 3),
    ]
    for (mm, dd), name, lvl in yearly:
        try:
            day = datetime(y, mm, dd).date()
        except ValueError:
            continue
        if -1 <= (day - d).days <= 7:
            out.append(CalendarItem(name, lvl, f"{day:%d %b}"))
    if m == 8 and d.day >= 18:
        out.append(CalendarItem("Jackson Hole central bank symposium (late August)", 4, "late Aug"))
    if (m == 12 and d.day >= 20) or (m == 1 and d.day <= 2):
        out.append(CalendarItem("Year-end holidays: thin liquidity", 2, "20 Dec - 2 Jan"))
    # Scheduled releases in the dated list, next 3 days
    for ev in EVENTS_2026:
        gap = (ev.at - now).total_seconds() / 3600
        if -2 <= gap <= 72:
            lvl = {"fomc": 5 if "projections" in ev.name else 4, "cpi": 4, "nfp": 4}.get(ev.kind, 3)
            out.append(CalendarItem(ev.name, lvl, f"{ev.at:%a %d %b %H:%M} UTC"))
    return sorted(out, key=lambda c: -c.level)


def calendar_text(now: datetime) -> str:
    """The calendar block a prompt starts with."""
    items = calendar_context(now)
    head = f"Now: {now:%A %d %B %Y, %H:%M} UTC."
    if not items:
        return head + " No scheduled market events nearby."
    return head + "\n" + "\n".join(f"- [L{c.level}] {c.name} ({c.when})" for c in items)
