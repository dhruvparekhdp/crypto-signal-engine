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
