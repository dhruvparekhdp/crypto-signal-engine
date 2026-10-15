"""
Headline -> (score, confidence, event_type, symbol) without an LLM call per headline.

The Hugging Face Space's financial-news classifier (FinBERT) gives positive / negative / neutral probabilities for
a batch of headlines in one call. FinBERT does not know what KIND of event a headline is, or which coin it is about,
so those come from the rules here. Together they replace the per-headline Groq call that used a few hundred requests
a day of the shared free quota (collectors/hermes.py SCORING prompt is the reference for the meaning of each field).
"""
from __future__ import annotations

import re

EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("hack", ("hack", "exploit", "drained", "stolen", "breach", "insolven", "bankrupt", "depeg", "rug pull")),
    ("rate_decision", ("rate cut", "rate hike", "fomc", "fed decision", "interest rate", "basis points", "bps",
                       "powell", "rate decision", "holds rates", "ecb", "boj")),
    ("inflation_data", ("cpi", "inflation", "pce", "ppi", "consumer prices")),
    ("jobs_data", ("payroll", "nfp", "jobless", "unemployment", "jobs report", "labor market")),
    ("war", ("war", "missile", "invasion", "airstrike", "military", "attack on", "sanction", "ceasefire")),
    ("tariff", ("tariff", "trade war", "import duty", "export ban")),
    ("regulation", ("sec ", "sec's", "cftc", "regulat", "lawsuit", "ban on", "crackdown", "court", "approval",
                    "approves", "legislation", "bill ", "mica", "license")),
    ("etf_flow", ("etf", "inflow", "outflow", "grayscale", "blackrock")),
    ("liquidation", ("liquidat", "short squeeze", "long squeeze", "open interest", "funding rate")),
    ("exchange_outage", ("outage", "halts withdrawals", "suspends withdrawals", "downtime", "maintenance")),
    ("adoption", ("adopt", "partnership", "treasury", "buys bitcoin", "acquires", "integrat", "launches",
                  "listing", "lists ")),
]
NOISE_PATTERNS = re.compile(
    r"price prediction|could (reach|hit|soar|surge|crash)|will (bitcoin|btc|eth|ethereum|xrp|solana) (reach|hit)|"
    r"what to (watch|expect)|top \d+|best crypto|analyst says|here'?s why|should you buy|to buy (now|today)|"
    r"\bvs\b.*which|technical analysis|price analysis|weekly recap|daily recap|newsletter",
    re.I)
MACRO = {"rate_decision", "inflation_data", "jobs_data", "war", "tariff"}
COINS = {
    "btcusdt": ("bitcoin", "btc"), "ethusdt": ("ethereum", "ether", "eth"), "solusdt": ("solana", "sol"),
    "xrpusdt": ("xrp", "ripple"), "dogeusdt": ("dogecoin", "doge"), "bnbusdt": ("bnb", "binance coin"),
    "adausdt": ("cardano", "ada"), "avaxusdt": ("avalanche", "avax"), "linkusdt": ("chainlink", "link"),
    "ltcusdt": ("litecoin", "ltc"), "bchusdt": ("bitcoin cash", "bch"), "suiusdt": ("sui",),
}


def event_type(headline: str) -> str:
    h = " " + headline.lower() + " "
    if NOISE_PATTERNS.search(h):
        return "noise"
    for name, words in EVENT_RULES:
        if any(w in h for w in words):
            return name
    if any(re.search(rf"\b{re.escape(n)}\b", h) for names in COINS.values() for n in names) or "crypto" in h:
        return "crypto_other"
    return "macro_other"


def symbol(headline: str) -> str:
    h = headline.lower()
    hits = [sym for sym, names in COINS.items() if any(re.search(rf"\b{re.escape(n)}\b", h) for n in names)]
    if "bitcoin cash" in h:
        hits = [s for s in hits if s != "btcusdt"]
    return hits[0] if len(hits) == 1 else "all"


def combine(probs: dict, headline: str) -> dict:
    """FinBERT probabilities + rules -> the fields the news pipeline stores."""
    ev = event_type(headline)
    pos, neg = float(probs.get("positive", 0)), float(probs.get("negative", 0))
    score = pos - neg
    conf = max(pos, neg)
    if ev == "noise":
        score, conf = 0.0, min(conf, 0.1)
    elif ev in ("crypto_other", "macro_other"):
        conf *= 0.6                                     # a vague headline is worth less than a named event
        if abs(score) < 0.3:
            ev, score, conf = "noise", 0.0, min(conf, 0.1)
    if ev == "hack":
        score = min(score, -0.3)                        # finance models sometimes read "recovered funds" upbeat
    sym = "all" if ev in MACRO else symbol(headline)
    return {"score": round(max(-1.0, min(1.0, score)), 3), "confidence": round(max(0.0, min(1.0, conf)), 3),
            "event_type": ev, "symbol": sym}


# ── LLM check of the headlines that matter (15 Oct 2026 bake-off) ─────────────
# FinBERT + rules let 42% of known noise through; Groq gpt-oss-20b caught 92% (gpt-oss-120b 100%) at ~1 s per
# 30 headlines. A false "high impact" headline pauses trading, so every headline the rules call high-impact or
# strong gets one batched LLM check; the rest keep the free FinBERT score.
REVIEW_SYSTEM = (
    "You check news for a crypto trading desk. For each numbered headline decide: relevant (could it move BTC or "
    "the major coins within 24 hours?), score from -1 (bearish for crypto) to +1 (bullish), confidence 0..1, and "
    "event_type from: {types}. Judge only from the headline; routine or off-topic items are noise with score 0. "
    "Reply with JSON only: {{\"items\": [{{\"i\": 1, \"relevant\": true, \"score\": -0.4, \"confidence\": 0.7, "
    "\"event_type\": \"etf_flow\"}}]}} with one entry for every headline.")


def needs_review(item: dict, high_impact: set[str], min_abs_score: float = 0.5) -> bool:
    """A FinBERT-scored headline worth an LLM check: tagged high-impact, or a strong score."""
    return item.get("event_type") in high_impact or abs(float(item.get("score") or 0)) >= min_abs_score


def apply_review(items: list[dict], reply_items: list, event_types: list[str]) -> int:
    """Overwrite score/confidence/event_type with the LLM's answer for each numbered headline (1-based, in order).
    Irrelevant -> noise with score 0. Malformed entries are ignored (the FinBERT score stays). Returns the count."""
    n = 0
    for x in reply_items or []:
        if not isinstance(x, dict):
            continue
        try:
            k = int(x.get("i")) - 1
            score = max(-1.0, min(1.0, float(x.get("score") or 0)))
            conf = max(0.0, min(1.0, float(x.get("confidence") or 0)))
        except (TypeError, ValueError):
            continue
        if not 0 <= k < len(items):
            continue
        rel = x.get("relevant")
        rel = rel if isinstance(rel, bool) else str(rel).lower() == "true"
        et = str(x.get("event_type") or "").strip().lower()
        it = items[k]
        if not rel or et == "noise":
            it.update(score=0.0, confidence=conf, event_type="noise")
        else:
            it.update(score=score, confidence=conf, event_type=et if et in event_types else it.get("event_type", "macro_other"))
        it["model"] = (it.get("model") or "") + "+llm"
        n += 1
    return n
