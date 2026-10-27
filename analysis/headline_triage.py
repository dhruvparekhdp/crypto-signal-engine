"""
Our own headline triage (owner, 18 Oct 2026, AI plan step 6): relevant or noise, event type and direction from the
headline's bge-small embedding - the vectors the duplicate filter (step 5) already fetches from our HF Space, so this
costs no extra call. Three logistic-regression heads trained by scripts/train_headline_triage.py on 1,679 headlines
labelled by Groq gpt-oss-120b; the weights are analysis/models/headline_triage.json.

Over five rolling time splits it let 19% of noise through (FinBERT + rules: 52%) and dropped 13% of real news (34%),
event type right 70% of the time (40%). FinBERT + rules stays as the fallback when there is no vector.

Safety: a headline this model calls high-impact (rate decision, inflation, jobs, war, tariff) gets its confidence
held just under the 0.7 that pauses new trades (analysis/event_calendar.HIGH_IMPACT_NEWS_CONFIDENCE). The existing
LLM check reviews every high-impact or strong headline, and only its confirmation can pause trading.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parent / "models" / "headline_triage.json"
HIGH_IMPACT = {"rate_decision", "inflation_data", "jobs_data", "war", "tariff"}
# 27 Oct 2026, the day before an FOMC: the model filed "Fed rate hikes are coming, but not this week: CNBC survey" and
# "Will Kevin Warsh Announce Surprise Fed Rate Hike?" as noise (it was trained before Warsh chaired the Fed). A headline
# that names the rate decision itself is kept as rate_decision; "what to expect" pieces stay noise (headline_rules).
FED_DECISION = re.compile(r"\bfomc\b|rate (hike|cut|decision)s?\b|fed decision|holds rates|\bwarsh\b|federal reserve",
                          re.I)
UNCONFIRMED_HIGH_IMPACT_CAP = 0.69          # below the 0.7 that turns a headline into a trading pause
MACRO = {"rate_decision", "inflation_data", "jobs_data", "war", "tariff", "macro_other"}


@lru_cache(maxsize=1)
def load(path: str = str(MODEL_PATH)) -> dict | None:
    """The trained heads, or None when the file is missing or broken (the caller falls back to FinBERT + rules)."""
    try:
        m = json.loads(Path(path).read_text())
        assert all(k in m for k in ("relevance", "event", "direction", "threshold", "dim"))
        return m
    except (OSError, ValueError, AssertionError):
        return None


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _scores(h: dict, x: list[float]) -> list[float]:
    return [sum(w * xi for w, xi in zip(row, x)) + b for row, b in zip(h["coef"], h["intercept"])]


def _softmax(z: list[float]) -> list[float]:
    m = max(z)
    e = [math.exp(v - m) for v in z]
    s = sum(e)
    return [v / s for v in e]


def classify(vec: list[float], headline: str, model: dict | None = None) -> dict | None:
    """{relevant, p_relevant, event_type, score, confidence, symbol} for one headline, or None without a model."""
    m = model or load()
    if m is None or not vec or len(vec) != m["dim"]:
        return None
    from analysis.headline_rules import NOISE_PATTERNS, symbol
    x = _unit(vec)
    p_rel = 1 / (1 + math.exp(-_scores(m["relevance"], x)[0]))           # binary head: one row, class True
    relevant = p_rel >= m["threshold"]
    fed = FED_DECISION.search(headline) is not None and not NOISE_PATTERNS.search(headline)
    relevant = relevant or fed
    ev_classes, ev_p = m["event"]["classes"], _softmax(_scores(m["event"], x))
    ranked = sorted(zip(ev_p, ev_classes), reverse=True)
    if not relevant:
        event, p_ev = "noise", 1 - p_rel
    elif fed:
        event = "rate_decision"
    else:
        p_ev, event = next(((p, c) for p, c in ranked if c != "noise"), ranked[0])
    if relevant:
        d = dict(zip(m["direction"]["classes"], _softmax(_scores(m["direction"], x))))
        score = d.get(1, 0.0) - d.get(-1, 0.0)
        confidence = (max(p_rel, m["threshold"]) if fed else p_rel) * max(d.values())
    else:
        score, confidence = 0.0, min(0.1, 1 - p_rel)
    if event == "hack":
        score = min(score, -0.3)                         # same rule as FinBERT's: hacks are never good news
    if event in HIGH_IMPACT:
        confidence = min(confidence, UNCONFIRMED_HIGH_IMPACT_CAP)        # only the LLM check can make it pause trading
    return {"relevant": relevant, "p_relevant": round(p_rel, 3), "event_type": event,
            "score": round(max(-1.0, min(1.0, score)), 3), "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "symbol": "all" if event in MACRO else symbol(headline)}
