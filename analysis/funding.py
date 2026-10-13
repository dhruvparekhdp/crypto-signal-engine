"""
Real funding for paper positions (owner, 13 Oct 2026: "charges are not calculated like trade fees, funding rate").

Perpetual futures settle funding every 8 hours (00:00, 08:00, 16:00 UTC on Delta India: product_specs
rate_exchange_interval = 28800). At each settlement a position pays notional x rate: longs pay a positive rate,
shorts receive it; a negative rate reverses that. The old model charged a flat 0.00655% per 8h in both directions.

Rates come from Delta India's public FUNDING:<SYMBOL> candles (hourly; the value is the funding rate in % per 8h).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

SETTLE_S = 8 * 3600


def settlements(opened_s: float, now_s: float) -> list[int]:
    """Funding settlement times (epoch seconds) strictly after opening and up to now."""
    first = (int(opened_s) // SETTLE_S + 1) * SETTLE_S
    return list(range(first, int(now_s) + 1, SETTLE_S))


def rate_at(rates: list[tuple[int, float]], t: int) -> float | None:
    """The rate (% per 8h) in force just before settlement time t: the latest hourly value at or before t."""
    best = None
    for ts, r in rates:
        if ts <= t:
            best = r
        else:
            break
    return best


def accrued(side: int, notional_inr: float, opened_s: float, now_s: float, rates: list[tuple[int, float]],
            fallback_pct: float = 0.00655) -> float:
    """Funding cost so far in INR (positive = paid, negative = received). `rates` sorted (epoch s, % per 8h).
    A settlement with no known rate uses the conservative flat fallback as a cost."""
    total = 0.0
    for t in settlements(opened_s, now_s):
        r = rate_at(rates, t)
        if r is None:
            total += notional_inr * fallback_pct / 100
        else:
            total += side * notional_inr * r / 100
    return total


_cache: dict[str, tuple[float, list[tuple[int, float]]]] = {}


async def fetch_rates(binance_symbol: str, start_s: int, end_s: int, client=None) -> list[tuple[int, float]]:
    """Hourly funding rates (epoch s, % per 8h) from Delta India, cached 10 minutes per symbol."""
    import httpx

    from execution.delta_india import INDIA_URL, USER_AGENT, binance_to_delta_symbol
    key = binance_symbol.upper()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600 and hit[1] and hit[1][0][0] <= start_s:
        return hit[1]
    sym = "FUNDING:" + binance_to_delta_symbol(key)
    own = client is None
    c = client or httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT})
    try:
        r = await c.get(f"{INDIA_URL}/v2/history/candles",
                        params={"symbol": sym, "resolution": "1h", "start": int(start_s) - 8 * 3600, "end": int(end_s)})
        r.raise_for_status()
        rows = r.json().get("result") or []
    finally:
        if own:
            await c.aclose()
    rates = sorted((int(x["time"]), float(x["close"])) for x in rows if x.get("close") is not None)
    _cache[key] = (time.time(), rates)
    return rates


def epoch(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
