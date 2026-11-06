"""
Free, keyless sentiment inputs, and how they move a signal's confidence.

Two feeds earn their place because both are free, need no API key, and both
measure something the price-derived indicators cannot see:

  Fear & Greed Index (alternative.me) — a 0-100 crowd-positioning gauge,
  updated daily. Extremes matter far more than the middle: 15 and 85 say
  something, 48 and 52 say nothing.

  Funding rate (the exchange itself) — what longs are paying shorts right now.
  Heavily positive funding means the crowd is crowded long and paying for the
  privilege, which is the condition that precedes a flush.

Design decision worth stating: neither of these fires a signal of its own.
They adjust the confidence of a signal that already exists, and confidence
sets position size. A sentiment reading is a reason to size up or down, not a
reason to trade — treating it as an entry trigger is how a daily-resolution
number ends up driving a thirty-minute decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

FEAR_GREED_URL = "https://api.alternative.me/fng/"


@dataclass(frozen=True)
class FearGreed:
    value: int                 # 0 = extreme fear, 100 = extreme greed
    classification: str

    @property
    def is_extreme(self) -> bool:
        """Only the tails carry information; the middle is noise."""
        return self.value <= 25 or self.value >= 75


@dataclass(frozen=True)
class SentimentAdjustment:
    """What sentiment does to a signal, and why — the reason is shown to you."""

    delta: float
    reason: str

    @property
    def is_material(self) -> bool:
        return abs(self.delta) >= 0.01


# Confidence moves in small steps. A sentiment reading is weak evidence
# compared with the setup itself, and letting it swing confidence by 0.10 would
# let a daily crowd gauge halve or double a position size on its own.
MAX_TOTAL_DELTA = 0.08
FEAR_GREED_DELTA = 0.04
FUNDING_DELTA = 0.04

# Funding above this is a crowded, paying-to-hold market. CoinDCX's own
# 8-hourly rate sat near 0.0066% in the reconciled ledger, so 0.03% is roughly
# five times normal rather than an arbitrary round number.
CROWDED_FUNDING_PER_8H = 0.0003


async def fetch_fear_greed(timeout: float = 10.0) -> FearGreed | None:
    """One value for the whole crypto market. Free, no key, updated daily."""
    from collectors.source_health import watching

    async with watching("fear_greed") as outcome:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(FEAR_GREED_URL, params={"limit": 1})
                if r.status_code != 200:
                    log.warning("fng.http", status=r.status_code)
                    outcome["error"] = f"HTTP {r.status_code}"
                    return None
                rows = r.json().get("data") or []
                if not rows:
                    outcome["error"] = "empty index"
                    return None
                value = FearGreed(value=int(rows[0]["value"]),
                                  classification=str(rows[0].get("value_classification", "")))
                outcome["ok"] = True
                return value
        except Exception as exc:
            log.warning("fng.failed", error=f"{type(exc).__name__}: {exc}")
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            return None


def fear_greed_adjustment(fg: FearGreed | None, direction: str) -> SentimentAdjustment:
    """
    Extreme fear favours longs, extreme greed favours shorts.

    This is contrarian on purpose, and it is the one place the system leans
    that way deliberately rather than by accident: the index measures crowd
    positioning, and a crowd already all-in has nobody left to buy from.
    """
    if fg is None or not fg.is_extreme:
        return SentimentAdjustment(0.0, "")

    crowd_is_fearful = fg.value <= 25
    helps = (direction == "long") if crowd_is_fearful else (direction == "short")
    mood = "extreme fear" if crowd_is_fearful else "extreme greed"
    if helps:
        return SentimentAdjustment(FEAR_GREED_DELTA, f"{mood} ({fg.value}) favours a {direction}")
    return SentimentAdjustment(-FEAR_GREED_DELTA, f"{mood} ({fg.value}) is against a {direction}")


def funding_adjustment(rate_per_8h: float | None, direction: str) -> SentimentAdjustment:
    """
    Crowded funding is evidence against the crowded side.

    Positive funding means longs pay shorts, so a heavily positive rate is a
    market of leveraged longs paying rent. That is both a crowding signal and
    a direct cost if we join them.
    """
    if rate_per_8h is None or abs(rate_per_8h) < CROWDED_FUNDING_PER_8H:
        return SentimentAdjustment(0.0, "")

    crowded_long = rate_per_8h > 0
    against_the_crowd = (direction == "short") if crowded_long else (direction == "long")
    side = "longs" if crowded_long else "shorts"
    pct = rate_per_8h * 100
    if against_the_crowd:
        return SentimentAdjustment(
            FUNDING_DELTA, f"{side} are paying {pct:.3f}% funding — crowded, and we are not")
    return SentimentAdjustment(
        -FUNDING_DELTA, f"{side} are paying {pct:.3f}% funding — we would be joining the crowd")


def adjust_confidence(
    base_confidence: float,
    direction: str,
    fg: FearGreed | None = None,
    funding_per_8h: float | None = None,
) -> tuple[float, list[str]]:
    """
    Apply every sentiment input, returning the new confidence and the reasons.

    Clamped twice on purpose: the total movement is capped so sentiment can
    never dominate the setup, and the result is capped at 0.95 so nothing ever
    presents as a certainty.
    """
    parts = [
        fear_greed_adjustment(fg, direction),
        funding_adjustment(funding_per_8h, direction),
    ]
    total = sum(p.delta for p in parts)
    total = max(-MAX_TOTAL_DELTA, min(MAX_TOTAL_DELTA, total))

    adjusted = max(0.0, min(0.95, base_confidence + total))
    reasons = [p.reason for p in parts if p.is_material and p.reason]
    return round(adjusted, 4), reasons
