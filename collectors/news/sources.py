"""
P1 sources (plan W6): official APIs and RSS only; keys via settings SecretStr.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

from config.settings import settings
from collectors.news.base import NewsCollector, PollResult
from collectors.news.http import fetch_json, fetch_text
from collectors.news.registry import setting_key_for


def _enabled(collector_id: str) -> bool:
    if not settings.news_engine_enabled:
        return False
    return bool(getattr(settings, setting_key_for(collector_id), False))


def _secret(val) -> str | None:
    if val is None:
        return None
    return val.get_secret_value() if hasattr(val, "get_secret_value") else str(val)


class EconomicCalendarCollector(NewsCollector):
    collector_id = "economic_calendar"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        key = _secret(settings.fmp_api_key)
        if not key:
            return self._mark_not_configured("FMP API key not configured")
        try:
            data = await fetch_json(
                f"https://financialmodelingprep.com/api/v3/economic_calendar?apikey={key}",
                fixture="economic_calendar_fmp.json",
            )
            rows = data if isinstance(data, list) else data.get("data") or []
            return self._mark_ok(len(rows), f"{len(rows)} calendar rows")
        except Exception as e:
            return self._mark_error(str(e))


class CentralBankRssCollector(NewsCollector):
    collector_id = "central_bank_rss"

    FEEDS = (
        ("fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("ecb", "https://www.ecb.europa.eu/rss/press.html"),
    )

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        total = 0
        try:
            for code, _url in self.FEEDS:
                xml = await fetch_text(_url, fixture=f"central_bank_{code}_rss.xml")
                root = ET.fromstring(xml)
                total += len(root.findall(".//item"))
            return self._mark_ok(total, f"{total} RSS items")
        except Exception as e:
            return self._mark_error(str(e))


class UsTreasuryFredCollector(NewsCollector):
    collector_id = "us_treasury_fred"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        key = _secret(settings.fred_api_key)
        if not key:
            return self._mark_not_configured("FRED API key not configured")
        try:
            data = await fetch_json(
                "https://api.stlouisfed.org/fred/series/observations"
                f"?series_id=DTSFDEF&api_key={key}&file_type=json&limit=5",
                fixture="fred_treasury_deficit.json",
            )
            obs = (data.get("observations") or []) if isinstance(data, dict) else []
            return self._mark_ok(len(obs), f"{len(obs)} fiscal observations")
        except Exception as e:
            return self._mark_error(str(e))


class PolymarketOddsCollector(NewsCollector):
    collector_id = "polymarket_odds"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            data = await fetch_json(
                "https://gamma-api.polymarket.com/markets?limit=20&active=true",
                fixture="polymarket_markets.json",
            )
            rows = data if isinstance(data, list) else data.get("data") or []
            return self._mark_ok(len(rows), f"{len(rows)} Polymarket markets")
        except Exception as e:
            return self._mark_error(str(e))


class KalshiOddsCollector(NewsCollector):
    collector_id = "kalshi_odds"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            data = await fetch_json(
                "https://api.elections.kalshi.com/trade-api/v2/markets?limit=20",
                fixture="kalshi_markets.json",
            )
            rows = (data.get("markets") or []) if isinstance(data, dict) else []
            return self._mark_ok(len(rows), f"{len(rows)} Kalshi markets")
        except Exception as e:
            return self._mark_error(str(e))


class BinanceLiquidationsCollector(NewsCollector):
    collector_id = "binance_liquidations"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            events = await fetch_json(
                "https://fapi.binance.com/fapi/v1/allForceOrders?limit=100",
                fixture="binance_force_orders.json",
            )
            rows = events if isinstance(events, list) else []
            buckets: dict[str, dict] = {}
            for ev in rows:
                sym = ev.get("symbol", "BTCUSDT")
                ts = int(ev.get("time", 0)) // 60000 * 60000
                key = f"{sym}:{ts}"
                b = buckets.setdefault(key, {"long_usd": 0.0, "short_usd": 0.0, "count": 0})
                px = float(ev.get("price") or 0)
                qty = float(ev.get("origQty") or 0)
                usd = px * qty
                side = (ev.get("side") or "").upper()
                if side == "SELL":
                    b["long_usd"] += usd
                else:
                    b["short_usd"] += usd
                b["count"] += 1
            return self._mark_ok(len(buckets), f"{len(buckets)} minute buckets")
        except Exception as e:
            return self._mark_error(str(e))


class DefillamaCollector(NewsCollector):
    collector_id = "defillama"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            data = await fetch_json("https://api.llama.fi/v2/chains", fixture="defillama_chains.json")
            rows = data if isinstance(data, list) else []
            return self._mark_ok(len(rows), f"{len(rows)} chain TVL rows")
        except Exception as e:
            return self._mark_error(str(e))


class CrossAssetPricesCollector(NewsCollector):
    collector_id = "cross_asset_prices"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            data = await fetch_json(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                fixture="cross_asset_prices.json",
            )
            n = len(data) if isinstance(data, dict) else 0
            return self._mark_ok(n, f"{n} cross-asset ticks")
        except Exception as e:
            return self._mark_error(str(e))


class RegionalRssCollector(NewsCollector):
    collector_id = "regional_rss"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            xml = await fetch_text(
                "https://feeds.reuters.com/reuters/worldNews",
                fixture="regional_rss.xml",
            )
            root = ET.fromstring(xml)
            items = root.findall(".//item")
            return self._mark_ok(len(items), f"{len(items)} regional headlines")
        except Exception as e:
            return self._mark_error(str(e))


class Gdelt15mCollector(NewsCollector):
    """Latest GDELT 15-min export — polled by low-priority worker (W13)."""

    collector_id = "gdelt_15m"

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            # Production lists http://data.gdeltproject.org/gdeltv2/lastupdate.txt
            meta = await fetch_text(
                "http://data.gdeltproject.org/gdeltv2/lastupdate.txt",
                fixture="gdelt_lastupdate.txt",
            )
            line = (meta.strip().splitlines() or [""])[-1]
            return self._mark_ok(1 if line else 0, line[:120])
        except Exception as e:
            return self._mark_error(str(e))


def build_collectors() -> list[NewsCollector]:
    return [
        EconomicCalendarCollector(),
        CentralBankRssCollector(),
        UsTreasuryFredCollector(),
        PolymarketOddsCollector(),
        KalshiOddsCollector(),
        BinanceLiquidationsCollector(),
        DefillamaCollector(),
        CrossAssetPricesCollector(),
        RegionalRssCollector(),
        Gdelt15mCollector(),
    ]
