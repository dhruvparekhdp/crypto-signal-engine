"""
Same story, many feeds (owner, 18 Oct 2026, AI plan step 5). One event arrives from five to ten desks; counted as ten
"new" headlines it trips the AI triggers (briefing on new news, event monitor on 3+ headlines), fills the 40-headline
news block in every prompt with one story, and weighs that story ten times in sentiment.

A headline whose meaning (bge-small embedding from our HF Space, cosine similarity) is within `threshold` of one already
seen in the last `window_hours` is a duplicate of it. It is still stored - with duplicate_of set - so nothing is lost
and counts stay honest, but readers of "the news" skip it. When the Space cannot embed, an exact match on the
normalised text is the fallback, so dedupe degrades and never blocks a headline.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def normalise(text: str) -> str:
    """Lowercase letters and digits only: 'Bitcoin ETF sees $500M inflows!' == 'bitcoin etf sees 500m inflows'."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two unit vectors."""
    return sum(x * y for x, y in zip(a, b))


@dataclass
class _Seen:
    external_id: str
    published_at: datetime
    norm: str
    vec: list[float] | None


@dataclass
class RecentHeadlines:
    """The originals of the last window_hours, in memory (warmed from the database after a restart)."""
    threshold: float = 0.9
    window_hours: float = 12.0
    items: list[_Seen] = field(default_factory=list)
    warmed: bool = False

    def prune(self, now: datetime) -> None:
        lo = now - timedelta(hours=self.window_hours)
        self.items = [s for s in self.items if s.published_at >= lo]

    def match(self, text: str, vec: list[float] | None, published_at: datetime) -> _Seen | None:
        """The original this headline repeats, or None. Meaning first; exact text when there is no vector."""
        norm = normalise(text)
        span = timedelta(hours=self.window_hours)
        best, best_c = None, self.threshold
        for s in self.items:
            if abs(published_at - s.published_at) > span:
                continue
            if norm and norm == s.norm:
                return s
            if vec is not None and s.vec is not None:
                c = cosine(vec, s.vec)
                if c >= best_c:
                    best, best_c = s, c
        return best

    def add(self, external_id: str, text: str, vec: list[float] | None, published_at: datetime) -> None:
        self.items.append(_Seen(external_id, published_at, normalise(text), vec))


def naive_utc(dt: datetime) -> datetime:
    """Database times are naive UTC; feed times may carry a zone."""
    from datetime import UTC
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def mark_duplicates(headlines: list, vectors: list[list[float]] | None, recent: RecentHeadlines,
                    now: datetime) -> int:
    """Set .duplicate_of on every headline that repeats an earlier one (in the window or earlier in this batch);
    the rest become originals. Returns how many were duplicates. Headlines need external_id, headline, published_at."""
    now = naive_utc(now)
    recent.prune(now)
    vecs = [unit(v) for v in vectors] if vectors and len(vectors) == len(headlines) else [None] * len(headlines)
    dupes = 0
    for h, v in zip(headlines, vecs):
        when = naive_utc(h.published_at or now)
        orig = recent.match(h.headline, v, when)
        if orig is not None:
            h.duplicate_of = orig.external_id
            dupes += 1
        else:
            h.duplicate_of = ""
            recent.add(h.external_id, h.headline, v, when)
    return dupes
