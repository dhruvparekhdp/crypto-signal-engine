"""
What Delta India lists - the watchlist search box (owner, 18 Oct 2026: "why still binance in watchlist... change to
delta search and price"). Real money trades on Delta, so a pair goes on the watchlist only if Delta has its perpetual.

One public call (/v2/tickers, perpetuals only, cached 60 s) gives every contract with its name, mark price, 24h change
and 24h turnover, so "gold", "xau", "xaut" and "tether gold" all find XAUTUSD. Symbols come back Binance-shaped
(xautusdt), the engine's internal name. No key, no account.
"""
from __future__ import annotations

import time

import httpx
import structlog

log = structlog.get_logger()

INDIA_URL = "https://api.india.delta.exchange"
CACHE_SECONDS = 60
POPULAR = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "SUI", "XAUT", "TIA")
# names people type that are not in the ticker or the contract description
NAME_HINTS: dict[str, tuple[str, ...]] = {
    "gold": ("XAUT", "PAXG"), "xau": ("XAUT", "PAXG"), "silver": ("XAG",), "ripple": ("XRP",),
    "binance coin": ("BNB",), "dogecoin": ("DOGE",), "ether": ("ETH",), "polygon": ("POL", "MATIC"),
}

_cache: dict = {"t": 0.0, "rows": None}


def parse_tickers(rows: list) -> dict[str, dict]:
    """Delta ticker rows -> {base asset: pair facts}, perpetuals quoted in USD only."""
    out: dict[str, dict] = {}
    for r in rows or []:
        sym = str(r.get("symbol") or "").upper()
        if r.get("contract_type") not in (None, "perpetual_futures") or not sym.endswith("USD") or sym.endswith("USDT"):
            continue
        base = str(r.get("underlying_asset_symbol") or sym[:-3]).upper()
        try:
            price = float(r.get("mark_price") or r.get("close") or 0)
            o, c = float(r.get("open") or 0), float(r.get("close") or 0)
            turnover = float(r.get("turnover_usd") or r.get("turnover") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out[base] = {"delta_symbol": sym, "symbol": (sym[:-3] + "USDT").lower(), "base": base,
                     "name": str(r.get("description") or ""), "price": price,
                     "change_pct": round((c / o - 1) * 100, 3) if o > 0 and c > 0 else None,
                     "quote_volume_24h": turnover}
    return out


def rank(query: str, pairs: dict[str, dict], limit: int = 20) -> list[str]:
    """Base assets matching a query, best first: exact ticker, a known name, ticker prefix, contract name, substring."""
    q = query.strip().lower()
    if not q:
        return []
    qu = q.upper().removesuffix("USDT").removesuffix("USD") or q.upper()
    hinted = {b for name, bases in NAME_HINTS.items() if q in name or name in q for b in bases}
    scored: list[tuple[int, str]] = []
    for base, p in pairs.items():
        name = p["name"].lower()
        if base == qu:
            score = 0
        elif base in hinted:
            score = 1
        elif base.startswith(qu):
            score = 2
        elif len(q) >= 3 and q in name:
            score = 3
        elif qu in base:
            score = 4
        else:
            continue
        scored.append((score, base))
    scored.sort(key=lambda t: (t[0], -pairs[t[1]]["quote_volume_24h"], t[1]))
    return [b for _, b in scored[:limit]]


async def load(timeout: float = 15.0) -> dict[str, dict] | None:
    """Every Delta India perpetual, cached 60 s. None when Delta did not answer (unknown, not 'nothing listed')."""
    if _cache["rows"] is not None and time.time() - _cache["t"] < CACHE_SECONDS:
        return _cache["rows"]
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "crypto-signal-engine"}) as c:
            r = await c.get(f"{INDIA_URL}/v2/tickers", params={"contract_types": "perpetual_futures"})
        r.raise_for_status()
        pairs = parse_tickers(r.json().get("result") or [])
    except Exception as e:  # noqa: BLE001 - the caller falls back to Binance
        log.warning("delta_symbols_unavailable", error=str(e)[:120])
        return _cache["rows"]
    if pairs:
        _cache.update(t=time.time(), rows=pairs)
    return pairs or _cache["rows"]


async def search(query: str, watchlist: set[str], limit: int = 20) -> dict:
    """The payload behind the watchlist search box (same shape as the old Binance search)."""
    pairs = await load()
    if pairs is None:
        return {"results": [], "available": False, "venue": "Delta India",
                "error": "Delta India did not answer from this server; try again shortly."}
    q = (query or "").strip()
    bases = [b for b in POPULAR if b in pairs][:limit] if len(q) < 2 else rank(q, pairs, limit)
    return {"available": True, "venue": "Delta India", "results": [
        {**{k: pairs[b][k] for k in ("symbol", "delta_symbol", "base", "name", "price", "change_pct",
                                      "quote_volume_24h")},
         "pair": f"{b}/USD", "futures": True, "on_watchlist": pairs[b]["symbol"] in watchlist}
        for b in bases]}
