"""
Hermes — reads the news, scores it locally, and posts the scores to the engine.

Why it is a separate agent
--------------------------
The engine runs on a rented box in Sydney. The model that scores headlines
runs on a laptop in Mumbai. Hermes is the thing that sits with the model: it
fetches, scores, and pushes the result to `/api/sentiment/ingest`, which was
written for exactly this and has been waiting for a sender ever since.

Push rather than pull, because the laptop is behind a home router the engine
cannot dial into.

Why this exists at all
----------------------
`sentiment_score` is 0.0 on all 35,637 snapshots in the archive. The news
pipeline has never run: it was wired to CryptoPanic, which needs a token that
was never issued, so the field has been a column of zeros since the schema was
written — and every signal has been generated with no idea what the world was
doing.

The week of 12-19 September makes the cost of that concrete. The two moves
that decided it were a Fed rate decision and a short squeeze, and the system
traded through both with no more context than an RSI reading.

Free and keyless, on purpose
----------------------------
Everything here is RSS. No API key, no quota, no signup, nothing to expire
quietly at 3am. The Federal Reserve publishes its own press releases this way,
which is the single most valuable feed in the list: it is where a rate
decision appears first and it is free.

Scoring belongs on the laptop
-----------------------------
A headline is a short, structured classification — a score, a tag, a symbol —
which is the shape of work a small local model does well and a frontier model
is wasted on. Hundreds of headlines a day through a metered API is a bill;
through a model on hardware you already own it is electricity.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree import ElementTree

import httpx
import structlog

log = structlog.get_logger()

# Keyless RSS. Ordered roughly by how much a headline from each one has ever
# moved a price: the central bank first, the trade press after.
FEEDS: list[tuple[str, str]] = [
    ("federal_reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("bitcoinmagazine", "https://bitcoinmagazine.com/feed"),
    ("yahoo_finance", "https://finance.yahoo.com/news/rssindex"),
]

# A closed vocabulary, for the reason every other vocabulary in this codebase
# is closed: "some tariff thing" and "trade policy" aggregate to nothing, and
# `tariff` aggregates to a count you can ask questions of later.
EVENT_TYPES = [
    "rate_decision", "inflation_data", "jobs_data", "war", "tariff",
    "regulation", "etf_flow", "liquidation", "hack", "adoption",
    "exchange_outage", "macro_other", "crypto_other", "noise",
]

# Which events have ever been worth standing aside for. Used by the caller to
# decide on a blackout, not by the scorer.
HIGH_IMPACT = {"rate_decision", "inflation_data", "jobs_data", "war", "tariff"}

# Which coin a headline is about is a word match against the WATCHLIST, not
# a list kept here: the engine's watchlist is the one list everything reads,
# and scripts/hermes.py fetches it each pass. A regex is free, deterministic,
# and cannot attribute a story to a coin that is not being followed.
#
# Names people write for a base asset. The ticker itself is matched too, but
# only in capitals ("SOL", not "sol"), because many tickers are also words.
BASE_NAMES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin",),
    "ETH": ("ethereum", "ether"),
    "SOL": ("solana",),
    "XRP": ("ripple", "xrp"),
    "LTC": ("litecoin",),
    "BCH": ("bitcoin cash",),
    "BNB": ("binance coin", "bnb"),
    "DOGE": ("dogecoin",),
    "ADA": ("cardano",),
    "LINK": ("chainlink",),
    "DOT": ("polkadot",),
    "AVAX": ("avalanche",),
    "TRX": ("tron",),
    "TON": ("toncoin",),
    "SHIB": ("shiba inu",),
    "PEPE": ("pepe",),
    "SUI": ("sui network",),
    "PAXG": ("gold", "bullion", "pax gold"),
    "XAUT": ("gold", "bullion", "tether gold"),
    "XAU": ("gold", "bullion"),
}

# Used until the engine's watchlist has been fetched, and when it cannot be.
DEFAULT_WATCHLIST = ("btcusdt", "ethusdt", "solusdt", "xrpusdt", "ltcusdt",
                     "bchusdt", "xauusdt")


def _base(symbol: str) -> str:
    s = symbol.upper()
    return s[:-4] if s.endswith("USDT") and len(s) > 4 else s


def words_for(symbols) -> dict[str, tuple[tuple[str, ...], str]]:
    """symbol -> (lower-case names, upper-case ticker) for each watchlist symbol."""
    out: dict[str, tuple[tuple[str, ...], str]] = {}
    for sym in symbols:
        base = _base(sym)
        out[sym.lower()] = (BASE_NAMES.get(base, ()), base if len(base) >= 3 else "")
    return out


_watch = words_for(DEFAULT_WATCHLIST)


def set_watchlist(symbols) -> None:
    """Match headlines against these symbols from now on. Empty keeps the old list."""
    global _watch
    symbols = [s for s in symbols if s]
    if symbols:
        _watch = words_for(symbols)


@dataclass
class Headline:
    external_id: str
    headline: str
    source: str
    url: str
    published_at: datetime | None = None
    symbol: str = "all"
    score: float = 0.0
    confidence: float = 0.0
    event_type: str = "noise"
    model: str = ""

    def as_payload(self) -> dict:
        return {
            "external_id": self.external_id,
            "symbol": self.symbol,
            "headline": self.headline,
            "source": self.source,
            "url": self.url,
            "score": self.score,
            "confidence": self.confidence,
            "event_type": self.event_type,
            "model": self.model,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass
class Batch:
    headlines: list[Headline] = field(default_factory=list)
    feeds_read: int = 0
    feeds_failed: int = 0

    @property
    def high_impact(self) -> list[Headline]:
        return [h for h in self.headlines if h.event_type in HIGH_IMPACT]


def stable_id(url: str, title: str) -> str:
    """
    The dedup key, and it has to survive a retry.

    The ingest endpoint deduplicates on this, so it must be a property of the
    story rather than of the fetch — a timestamp or a random id would let a
    retried batch double-count one headline, and a sentiment spike that never
    happened is worse than a missed one.
    """
    return hashlib.sha256(f"{url.strip()}|{title.strip()}".encode()).hexdigest()[:40]


def _text(node, *names: str) -> str:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def _parse_date(raw: str) -> datetime | None:
    """RSS dates come in several flavours and none of them are ISO."""
    if not raw:
        return None
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed else None
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # Convert, don't strip: an Atom time of 09:00+05:30 is 03:30 UTC.
        return (dt.astimezone(UTC) if dt.tzinfo else dt).replace(tzinfo=None)
    except ValueError:
        return None


def parse_feed(xml: str, source: str, limit: int = 25) -> list[Headline]:
    """
    Pull headlines out of RSS or Atom.

    Both, because the Fed publishes Atom and the crypto press publishes RSS,
    and a reader that handles only one of them silently drops the feed that
    matters most.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        log.warning("hermes_feed_unparseable", source=source, error=str(exc)[:120])
        return []

    out: list[Headline] = []
    items = root.iter("item")
    for item in items:
        title = _text(item, "title")
        link = _text(item, "link")
        if title and link:
            out.append(Headline(stable_id(link, title), title, source, link,
                                _parse_date(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date"))))
        if len(out) >= limit:
            return out

    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{ns}entry"):
        title = _text(entry, f"{ns}title")
        link_el = entry.find(f"{ns}link")
        link = link_el.get("href", "") if link_el is not None else ""
        if title and link:
            out.append(Headline(stable_id(link, title), title, source, link,
                                _parse_date(_text(entry, f"{ns}updated", f"{ns}published"))))
        if len(out) >= limit:
            break
    return out


def guess_symbol(title: str) -> str:
    """
    Which instrument a headline is about, by word match.

    Deliberately not the model's job. A regex cannot invent a symbol that is
    not on the watchlist, and a headline naming no coin is macro — which is
    `all`, and `all` is the one that matters for a rate decision.
    """
    low = f" {title.lower()} "
    best, best_len = "all", 0
    for symbol, (names, ticker) in _watch.items():
        for name in names:
            # The longest name wins, so "Bitcoin Cash" is BCH, not BTC.
            if len(name) > best_len and re.search(rf"\b{re.escape(name)}\b", low):
                best, best_len = symbol, len(name)
        if ticker and len(ticker) > best_len and re.search(rf"\b{re.escape(ticker)}\b", title):
            best, best_len = symbol, len(ticker)
    return best


async def fetch_feeds(timeout: float = 20.0,
                      feeds: list[tuple[str, str]] | None = None) -> Batch:
    """
    Read every feed. One being down costs that feed and nothing else.
    """
    batch = Batch()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "hermes/1.0"}) as client:
        for source, url in (feeds or FEEDS):
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    log.warning("hermes_feed_status", source=source, status=resp.status_code)
                    batch.feeds_failed += 1
                    continue
                found = parse_feed(resp.text, source)
                for headline in found:
                    headline.symbol = guess_symbol(headline.headline)
                batch.headlines.extend(found)
                batch.feeds_read += 1
            except Exception as exc:
                log.warning("hermes_feed_failed", source=source, error=str(exc)[:140])
                batch.feeds_failed += 1

    seen: set[str] = set()
    unique = []
    for headline in batch.headlines:
        if headline.external_id not in seen:
            seen.add(headline.external_id)
            unique.append(headline)
    batch.headlines = unique
    log.info("hermes_fetched", read=batch.feeds_read, failed=batch.feeds_failed,
             headlines=len(unique))
    return batch


SCORING_SYSTEM = (
    "Score a news headline for a crypto trading desk. One headline, one JSON "
    "object, nothing else.\n\n"
    "score: -1.0 to 1.0, how this moves risk assets. A rate HIKE is negative. "
    "A rate CUT is positive. War and tariffs are negative. ETF inflows and "
    "adoption are positive. An exchange hack is negative.\n\n"
    "confidence: 0.0 to 1.0. Be honest. A vague headline about 'experts "
    "predicting' deserves 0.1, a stated Fed decision deserves 0.9.\n\n"
    "Most headlines are noise. Price-prediction pieces, opinion, 'what to "
    "watch', anything about a token nobody trades — score those 0.0 with "
    "event_type 'noise'. A scorer that finds meaning in everything is a "
    "scorer nobody can act on.\n\n"
    f"event_type, use only these: {', '.join(EVENT_TYPES)}\n\n"
    'JSON only: {"score": 0.0, "confidence": 0.0, "event_type": "noise"}'
)


async def score_headline(headline: Headline) -> Headline:
    """
    Ask the model what a headline means. Returns the headline either way.

    An unscored headline is still worth storing — it is a record that the
    story existed at that hour, and a later pass can score it. What must not
    happen is a failed scorer dropping the news entirely.
    """
    from collectors.llm_client import ask_json

    try:
        reply = await ask_json("news_scoring", SCORING_SYSTEM,
                               f"{headline.source}: {headline.headline}",
                               max_tokens=300, temperature=0.0, timeout=30.0)
    except Exception as exc:
        log.warning("hermes_score_failed", error=str(exc)[:140])
        return headline

    if not reply:
        return headline

    try:
        headline.score = max(-1.0, min(1.0, float(reply.data.get("score") or 0.0)))
        headline.confidence = max(0.0, min(1.0, float(reply.data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        return headline

    event = str(reply.data.get("event_type") or "").strip().lower()
    headline.event_type = event if event in EVENT_TYPES else "noise"
    headline.model = reply.served_by
    return headline
