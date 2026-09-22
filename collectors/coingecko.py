from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from analysis.crypto_state_store import CryptoStateStore
from config.settings import settings

log = structlog.get_logger()

_API_BASE = "https://api.coingecko.com/api/v3"

# Static base-symbol -> CoinGecko coin id map for common coins (covers the
# default watchlist + most likely additions). Anything not listed here is
# resolved dynamically via /search and cached for the life of the process.
_SYMBOL_TO_ID = {
    "btc": "bitcoin", "eth": "ethereum", "bnb": "binancecoin", "sol": "solana",
    "xrp": "ripple", "doge": "dogecoin", "ada": "cardano", "avax": "avalanche-2",
    "shib": "shiba-inu", "dot": "polkadot", "link": "chainlink", "trx": "tron",
    "near": "near", "sui": "sui", "apt": "aptos", "uni": "uniswap",
    "ltc": "litecoin", "pepe": "pepe", "fet": "fetch-ai", "render": "render-token",
    "icp": "internet-computer", "bch": "bitcoin-cash", "kas": "kaspa",
    "pol": "matic-network", "matic": "matic-network", "etc": "ethereum-classic",
    "xlm": "stellar", "tao": "bittensor", "inj": "injective-protocol",
    "stx": "blockstack", "fil": "filecoin", "imx": "immutable-x", "arb": "arbitrum",
    "vet": "vechain", "sei": "sei-network", "ftm": "fantom", "rune": "thorchain",
    "floki": "floki", "bonk": "bonk", "wif": "dogwifcoin", "grt": "the-graph",
    "aave": "aave", "algo": "algorand", "sand": "the-sandbox", "mana": "decentraland",
    "flow": "flow", "theta": "theta-token", "egld": "elrond-erd-2", "qnt": "quant-network",
    "axs": "axie-infinity", "gala": "gala",
}


def _base_symbol(pair: str) -> str:
    """Strip the quote asset from a Binance-style pair, e.g. 'btcusdt' -> 'btc'."""
    p = pair.strip().lower()
    for quote in ("usdt", "busd", "usdc"):
        if p.endswith(quote) and len(p) > len(quote):
            return p[: -len(quote)]
    return p


class CoinGeckoCollector:
    """
    REST-polling crypto price collector using CoinGecko's free API.

    Default crypto price source — Binance's WebSocket API returns HTTP 451
    (geoblocked) from most cloud-hosted IPs including Render's, so it can't
    be relied on there. One batched /coins/markets call covers the entire
    watchlist per poll, so even the unauthenticated rate limit is nowhere
    close to a concern at a 60s+ interval.
    """

    def __init__(self, store: CryptoStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self._id_cache: dict[str, str | None] = {}

    def _headers(self) -> dict:
        if settings.coingecko_api_key:
            return {"x-cg-demo-api-key": settings.coingecko_api_key}
        return {}

    async def _resolve_id(self, client: httpx.AsyncClient, base: str) -> str | None:
        if base in self._id_cache:
            return self._id_cache[base]
        if base in _SYMBOL_TO_ID:
            self._id_cache[base] = _SYMBOL_TO_ID[base]
            return self._id_cache[base]
        try:
            resp = await client.get(
                f"{_API_BASE}/search", params={"query": base}, headers=self._headers()
            )
            if resp.status_code == 200:
                coins = resp.json().get("coins", [])
                # Ranked coins only. Sorting unranked ones to the back with
                # 999_999 still let them win when nothing ranked matched,
                # which is how XAUUSDT resolved to a dead token quoting gold
                # at $0.00004049 against a real $4,341 — a price that then
                # poisoned the candle its ATR was measured from. No match at
                # all is a safe answer; a wrong one is not.
                matches = [c for c in coins
                           if c.get("symbol", "").lower() == base
                           and c.get("market_cap_rank")]
                if matches:
                    matches.sort(key=lambda c: c["market_cap_rank"])
                    coin_id = matches[0]["id"]
                    self._id_cache[base] = coin_id
                    log.info("coingecko_symbol_resolved", symbol=base, coin_id=coin_id)
                    return coin_id
        except Exception as exc:
            log.warning("coingecko_search_failed", symbol=base, error=str(exc))
        self._id_cache[base] = None
        return None

    async def fetch(self) -> None:
        symbols = await self.store.get_symbols()
        if not symbols:
            return

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                id_map: dict[str, str] = {}  # coin_id -> our symbol
                for sym in symbols:
                    coin_id = await self._resolve_id(client, _base_symbol(sym))
                    if coin_id:
                        id_map[coin_id] = sym

                if not id_map:
                    log.warning("coingecko_no_symbols_resolved", symbols=symbols)
                    return

                resp = await client.get(
                    f"{_API_BASE}/coins/markets",
                    params={
                        "vs_currency": "usd",
                        "ids": ",".join(id_map.keys()),
                        "price_change_percentage": "24h",
                    },
                    headers=self._headers(),
                )

                if resp.status_code != 200:
                    self._consecutive_failures += 1
                    log.warning("coingecko_bad_status", status=resp.status_code,
                                body=resp.text[:200])
                    return

                rows = resp.json()
                now = datetime.now(timezone.utc)
                updated = 0
                for row in rows:
                    sym = id_map.get(row.get("id", ""))
                    if not sym:
                        continue
                    price = float(row.get("current_price") or 0.0)
                    if price <= 0:
                        continue
                    await self.store.update_from_rest(
                        symbol=sym,
                        price=price,
                        high_24h=float(row.get("high_24h") or price),
                        low_24h=float(row.get("low_24h") or price),
                        volume_24h=float(row.get("total_volume") or 0.0),
                        change_24h_pct=float(row.get("price_change_percentage_24h") or 0.0),
                        timestamp=now,
                    )
                    updated += 1

                self._consecutive_failures = 0
                log.info("coingecko_poll_done", symbols=updated, requested=len(id_map))
        except Exception as exc:
            self._consecutive_failures += 1
            log.warning("coingecko_fetch_failed", error=str(exc))
