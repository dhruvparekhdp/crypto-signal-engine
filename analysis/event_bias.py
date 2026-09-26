"""
Trade through news with the crowd's bias, instead of pausing.

The owner (26 Sep): "don't wait for such decisions; if waiting for some
news, ask Groq for public sentiment, bull or bear, then continue trading".
The pause had also fired on an analyst's forecast ("UBS expects only one
Fed hike") scored as a rate decision, so it could stall trading for hours.

During an event window the engine now asks Groq (web search) what traders
expect and what it means for crypto over the next hours. Signals WITH the
bias go ahead; only signals AGAINST a confident bias are skipped. Until the
answer arrives, or if no model answers, the engine's own news-sentiment
score gives the bias, and a weak or neutral reading blocks nothing.
"""
from __future__ import annotations

import math

MIN_CONFIDENCE = 0.55          # below this a bias is advice, not a gate

SYSTEM = (
    "A market-moving event is happening or about to happen. Search the web for "
    "how traders and the public expect it to land and what it means for crypto "
    "(BTC and the large coins) over the next few hours: analyst previews, "
    "market pricing (e.g. CME FedWatch), crypto funding and social mood. Answer "
    "bull, bear or neutral with a confidence from 0 to 1. If the evidence is "
    "mixed or thin, say neutral; never guess a direction. Write any time in IST.\n\n"
    "JSON only:\n"
    '{"bias": "bull|bear|neutral", "confidence": 0.0-1.0, '
    '"reason": "one or two sentences with the evidence", "sources": ["site"]}'
)


def prompt(event_name: str, kind: str, when_ist: str) -> str:
    return (f"Event: {event_name} (type: {kind}), at {when_ist} IST.\n"
            "What is the public / market expectation, and is the likely effect on "
            "crypto over the next few hours bullish, bearish or neutral?")


def parse(data: dict) -> dict | None:
    """A clean {bias, confidence, reason, sources} or None."""
    if not isinstance(data, dict):
        return None
    bias = str(data.get("bias", "")).strip().lower()
    if bias not in ("bull", "bear", "neutral"):
        return None
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf)) if math.isfinite(conf) else 0.5
    return {"bias": bias, "confidence": round(conf, 2),
            "reason": str(data.get("reason", "")).strip()[:300],
            "sources": [str(s)[:60] for s in (data.get("sources") or [])][:5],
            "source": "groq"}


def from_sentiment(score: float) -> dict:
    """Fallback from the engine's news-sentiment score (-1..+1)."""
    if score >= 0.2:
        bias = "bull"
    elif score <= -0.2:
        bias = "bear"
    else:
        bias = "neutral"
    return {"bias": bias, "confidence": round(min(1.0, abs(score)), 2),
            "reason": f"news sentiment score {score:+.2f}", "sources": [],
            "source": "news_sentiment"}


def allows(direction: str, bias: dict | None) -> bool:
    """True unless the signal fights a confident bias."""
    if not bias or bias.get("bias") == "neutral":
        return True
    if bias.get("confidence", 0) < MIN_CONFIDENCE:
        return True
    return (direction == "long") == (bias["bias"] == "bull")
