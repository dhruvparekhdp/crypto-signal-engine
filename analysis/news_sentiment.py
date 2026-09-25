"""
Turn stored headlines into the sentiment score the signals read.

The news scorer on the laptop has been filling `news_sentiment` with scored
headlines, and nothing read them back: `CryptoState.sentiment_score` was 0.0
on every snapshot, and the pre-trade reviewer was told "Sentiment: +0.00" on
every trade. This is the missing half.

How a pile of headlines becomes one number per coin
---------------------------------------------------
Each headline carries a score (-1 bearish .. +1 bullish) and the scorer's
confidence. Its weight is that confidence, times how much its source has
ever moved a price, times an exponential decay on its age — news is worth
half as much every three hours, so a morning headline has nearly vanished by
evening and a quiet day drifts back to zero on its own.

The weighted scores are averaged with a prior of weight 1.0 at zero. One
low-confidence headline therefore barely moves the score, while several
confident ones in agreement move it a lot. That shrinkage is what stops a
single sensational story from firing the sentiment detector, whose trigger
is |score| >= 0.35.

A coin's score is its own news plus half of the macro news ("all": the Fed,
tariffs, war). Macro moves everything, but it moves a coin less than a story
about that coin does.

Noise-tagged headlines count for nothing: they were scored as noise because
they say nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

HALF_LIFE_HOURS = 3.0
WINDOW_HOURS = 12
PRIOR_WEIGHT = 1.0
MACRO_SHARE = 0.5

# How much a headline from each source is trusted, relative to 1.0. The
# central bank's own release is the event itself, not commentary on it.
SOURCE_WEIGHT = {"federal_reserve": 1.5}


@dataclass(frozen=True)
class Reading:
    score: float      # -1 .. +1 after shrinkage
    items: int        # headlines that counted
    weight: float     # total evidence behind the score


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def aggregate(rows, now: datetime) -> Reading:
    """
    One reading from many scored headlines.

    Rows need score, confidence, source, event_type and published_at.
    """
    num = den = 0.0
    counted = 0
    for r in rows:
        if r.event_type == "noise" or not (_finite(r.score) and _finite(r.confidence)):
            continue
        if r.published_at is None:
            continue
        age_h = max(0.0, (now - r.published_at).total_seconds() / 3600)
        if age_h > WINDOW_HOURS:
            continue
        w = (max(0.0, min(1.0, r.confidence))
             * SOURCE_WEIGHT.get(r.source, 1.0)
             * 0.5 ** (age_h / HALF_LIFE_HOURS))
        if w <= 0:
            continue
        num += w * max(-1.0, min(1.0, r.score))
        den += w
        counted += 1
    score = num / (den + PRIOR_WEIGHT) if counted else 0.0
    return Reading(score=max(-1.0, min(1.0, score)), items=counted, weight=den)


def combine(coin: Reading, macro: Reading) -> float:
    """A coin's score: its own news, plus a share of the macro news."""
    return max(-1.0, min(1.0, coin.score + MACRO_SHARE * macro.score))


def per_symbol(rows, symbols, now: datetime) -> dict[str, tuple[float, int]]:
    """symbol -> (score, headlines behind it), for every followed symbol."""
    by_symbol: dict[str, list] = {}
    for r in rows:
        by_symbol.setdefault((r.symbol or "all").lower(), []).append(r)
    macro = aggregate(by_symbol.get("all", []), now)
    out = {}
    for sym in symbols:
        coin = aggregate(by_symbol.get(sym.lower(), []), now)
        out[sym.lower()] = (combine(coin, macro), coin.items + macro.items)
    return out
