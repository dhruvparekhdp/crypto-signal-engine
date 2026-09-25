"""
A 30-minute world briefing, researched on the web by Groq.

The reviewers used to be told "you have no news" — and for most of this
project that was true. Now Groq's compound model searches the web itself and
writes a short briefing: wars, tariffs, central banks, inflation data, big
crypto events, each with how much it matters for risk.

Three things happen with it:

1. It is stored (`market_briefings`), and every AI review records the id of
   the briefing it saw. Later, the local model on the laptop can relate each
   decision and each outcome to what the world was doing at the time.
2. Its events are written into `news_sentiment` as macro headlines, so they
   flow into the sentiment score and, when high-impact and confident, into
   the trading pause — the same path the RSS headlines take.
3. Its summary goes into the pre-trade, position and post-trade prompts.

Only web-searching models are in its chain. A model without search, asked
for today's news, invents some.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import structlog

from collectors.hermes import EVENT_TYPES

log = structlog.get_logger()

BRIEFING_SYSTEM = (
    "You brief a crypto trading desk. Search the web for what happened in the "
    "last 12 hours that moves crypto and risk assets: central banks and rate "
    "decisions, inflation and jobs data, wars and military escalation, tariffs "
    "and trade policy, sanctions, major regulation, ETF flows, big exchange "
    "hacks or outages, large liquidations. Only include things you found in "
    "search results from the last 12 hours. If nothing important happened, "
    "say so and return an empty list — do not pad it.\n\n"
    f"event_type, use only these: {', '.join(EVENT_TYPES)}\n\n"
    "Reply with JSON only, no prose around it:\n"
    '{"risk_tone": -1.0 to 1.0 (negative = risk-off for crypto), '
    '"summary": "3 to 5 plain sentences on what matters for crypto right now", '
    '"events": [{"headline": "short factual line", "event_type": "tag", '
    '"score": -1.0 to 1.0, "confidence": 0.0 to 1.0, '
    '"when": "ISO time in UTC if known, else empty", "source": "site name"}]}'
)


def _clamp(x, lo, hi, default=0.0) -> float:
    import math
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v)) if math.isfinite(v) else default


def parse_briefing(data: dict) -> tuple[float, str, list[dict]]:
    """(risk_tone, summary, events) from a model reply, cleaned."""
    tone = _clamp(data.get("risk_tone"), -1.0, 1.0)
    summary = str(data.get("summary") or "").strip()[:1500]
    events = []
    for e in (data.get("events") or [])[:15]:
        if not isinstance(e, dict):
            continue
        headline = str(e.get("headline") or "").strip()[:300]
        if not headline:
            continue
        etype = str(e.get("event_type") or "macro_other").strip().lower()
        events.append({
            "headline": headline,
            "event_type": etype if etype in EVENT_TYPES else "macro_other",
            "score": _clamp(e.get("score"), -1.0, 1.0),
            "confidence": _clamp(e.get("confidence"), 0.0, 1.0, 0.5),
            "when": str(e.get("when") or "").strip()[:40],
            "source": str(e.get("source") or "").strip()[:80],
        })
    return tone, summary, events


def as_news_items(events: list[dict], model: str) -> list[dict]:
    """Briefing events in the shape /api/sentiment/ingest and the repository take."""
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    items = []
    for e in events:
        # Keyed on the headline alone, so the same event found by two
        # briefings half an hour apart is stored once.
        key = hashlib.sha256(("briefing|" + e["headline"].lower()).encode()).hexdigest()[:32]
        items.append({
            "external_id": key,
            "symbol": "all",
            "headline": e["headline"],
            "source": "web_briefing" + (f":{e['source']}" if e["source"] else ""),
            "url": "",
            "score": e["score"],
            "confidence": e["confidence"],
            "event_type": e["event_type"],
            "model": model,
            "published_at": e["when"] or now,
        })
    return items


async def fetch_briefing():
    """Ask a web-searching model for the briefing. None if no model answered."""
    from collectors.llm_client import ask_json

    reply = await ask_json(
        "briefing", BRIEFING_SYSTEM,
        f"Current time: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}. Brief me.",
        max_tokens=1500, temperature=0.2, timeout=90.0)
    if not reply or not isinstance(reply.data, dict):
        return None
    tone, summary, events = parse_briefing(reply.data)
    log.info("market_briefing", tone=round(tone, 2), events=len(events),
             served_by=reply.served_by, latency_ms=reply.latency_ms)
    return tone, summary, events, reply.served_by, reply.latency_ms


def context_block(briefing, headlines, symbol: str, limit: int = 6) -> str:
    """
    The news paragraph handed to a reviewer, and stored with its review.

    Briefing summary first (the world), then the freshest scored headlines
    for this coin and for the macro picture.
    """
    lines = []
    if briefing is not None:
        age_min = max(0, int((datetime.now(UTC).replace(tzinfo=None)
                              - briefing.created_at).total_seconds() // 60))
        lines.append(f"World briefing ({age_min} min old, risk tone {briefing.risk_tone:+.2f}): "
                     f"{briefing.summary}")
    picked = [h for h in headlines
              if (h.symbol or "all").lower() in (symbol.lower(), "all") and h.event_type != "noise"]
    for h in picked[:limit]:
        lines.append(f"- [{h.event_type}, {h.score:+.1f}] {h.headline[:140]}")
    return "\n".join(lines) if lines else "No news in the last 12 hours."
