"""
P1 sources (plan W6): official APIs and RSS only; keys via settings SecretStr.
"""
from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from config.settings import settings
from collectors.news.base import NewsCollector, PollResult
from collectors.news.fixtures import load_fixture, use_fixtures
from collectors.news.http import fetch_json, fetch_text
from collectors.news.registry import setting_key_for
from analysis.news.store import (
    insert_cross_asset_tick,
    insert_defi_snapshot,
    insert_fred_observation,
    insert_odds_snapshot,
    upsert_calendar_row,
    upsert_document,
    upsert_gdelt_batch_pointer,
)
from storage.news_models import NewsLiquidationMinute


def _enabled(collector_id: str) -> bool:
    if not settings.news_engine_enabled:
        return False
    return bool(getattr(settings, setting_key_for(collector_id), False))


def _secret(val) -> str | None:
    if val is None:
        return None
    return val.get_secret_value() if hasattr(val, "get_secret_value") else str(val)


def _rss_time(item) -> datetime | None:
    """The item's own publish time as naive UTC (31 Oct 2026: every item was stamped with the fetch time, so
    September's FOMC minutes showed as today's news). RSS pubDate, else Dublin Core / Atom dates."""
    from email.utils import parsedate_to_datetime

    raw = (item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")
           or item.findtext("{http://www.w3.org/2005/Atom}updated") or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)).replace(tzinfo=None)


def _parse_rss_items(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title:
            items.append({"title": title, "url": link, "body": desc, "published": _rss_time(item)})
    return items


class EconomicCalendarCollector(NewsCollector):
    collector_id = "economic_calendar"

    async def _fetch_rows(self) -> list[dict]:
        key = _secret(settings.fmp_api_key)
        if not key:
            raise RuntimeError("FMP API key not configured")
        data = await fetch_json(
            f"https://financialmodelingprep.com/api/v3/economic_calendar?apikey={key}",
            fixture="economic_calendar_fmp.json",
        )
        return data if isinstance(data, list) else list(data.get("data") or [])

    async def _fetch_count(self) -> int:
        if not _enabled(self.collector_id):
            return 0
        if not _secret(settings.fmp_api_key):
            raise RuntimeError("FMP API key not configured")
        return len(await self._fetch_rows())

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        if not _secret(settings.fmp_api_key):
            return self._mark_not_configured("FMP API key not configured")
        try:
            return self._mark_ok(await self._fetch_count(), "calendar rows fetched")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        rows = await self._fetch_rows()
        n = 0
        for r in rows:
            when = datetime.strptime(r.get("date", "2026-01-01")[:10], "%Y-%m-%d")
            ext = f"{r.get('date')}:{r.get('event', '')}:{r.get('country', '')}"
            await upsert_calendar_row(
                session,
                source_id=self.collector_id,
                external_id=ext[:120],
                title=r.get("event") or "Release",
                country=(r.get("country") or "")[:8],
                scheduled_at=when,
                consensus=str(r.get("consensus") or ""),
                previous=str(r.get("previous") or ""),
                actual=str(r.get("actual") or ""),
            )
            n += 1
        return n


class CentralBankRssCollector(NewsCollector):
    collector_id = "central_bank_rss"

    FEEDS = (
        ("fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("ecb", "https://www.ecb.europa.eu/rss/press.html"),
    )

    async def _fetch_items(self) -> list[dict]:
        items: list[dict] = []
        for code, url in self.FEEDS:
            xml = await fetch_text(url, fixture=f"central_bank_{code}_rss.xml")
            for it in _parse_rss_items(xml):
                it["source_id"] = f"{self.collector_id}:{code}"
                items.append(it)
        return items

    async def _fetch_count(self) -> int:
        if not _enabled(self.collector_id):
            return 0
        return len(await self._fetch_items())

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            return self._mark_ok(await self._fetch_count(), "RSS items")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        n = 0
        for it in await self._fetch_items():
            await upsert_document(
                session,
                source_id=it["source_id"],
                title=it["title"],
                body=it.get("body") or "",
                url=it.get("url") or "",
                published_at=it.get("published") or now,
            )
            n += 1
        return n


class UsTreasuryFredCollector(NewsCollector):
    collector_id = "us_treasury_fred"

    async def _fetch_obs(self) -> list[dict]:
        key = _secret(settings.fred_api_key)
        if not key:
            raise RuntimeError("FRED API key not configured")
        data = await fetch_json(
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=DTSFDEF&api_key={key}&file_type=json&limit=5",
            fixture="fred_treasury_deficit.json",
        )
        return list((data.get("observations") or []) if isinstance(data, dict) else [])

    async def _fetch_count(self) -> int:
        if not _enabled(self.collector_id):
            return 0
        if not _secret(settings.fred_api_key):
            raise RuntimeError("FRED API key not configured")
        return len(await self._fetch_obs())

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        if not _secret(settings.fred_api_key):
            return self._mark_not_configured("FRED API key not configured")
        try:
            return self._mark_ok(await self._fetch_count(), "observations")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        n = 0
        for ob in await self._fetch_obs():
            await insert_fred_observation(
                session,
                series_id="DTSFDEF",
                observation_date=str(ob.get("date") or ""),
                value=str(ob.get("value") or ""),
                payload=ob,
                captured_at=now,
            )
            n += 1
        return n


class PolymarketOddsCollector(NewsCollector):
    collector_id = "polymarket_odds"

    async def _fetch_markets(self) -> list:
        data = await fetch_json(
            "https://gamma-api.polymarket.com/markets?limit=20&active=true",
            fixture="polymarket_markets.json",
        )
        return data if isinstance(data, list) else list(data.get("data") or [])

    async def _fetch_count(self) -> int:
        return len(await self._fetch_markets()) if _enabled(self.collector_id) else 0

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            return self._mark_ok(await self._fetch_count(), "markets")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        n = 0
        for m in await self._fetch_markets():
            q = m.get("question") or m.get("id") or "market"
            mid = str(m.get("id") or m.get("condition_id") or q)[:120]
            prob = m.get("outcomePrices") or m.get("yes_prob")
            yes_prob = None
            if isinstance(prob, list) and prob:
                try:
                    yes_prob = float(prob[0])
                except (TypeError, ValueError):
                    yes_prob = None
            await insert_odds_snapshot(
                session,
                venue="polymarket",
                market_id=mid,
                title=q,
                yes_prob=yes_prob,
                payload=m,
                captured_at=now,
            )
            n += 1
        return n


class KalshiOddsCollector(NewsCollector):
    collector_id = "kalshi_odds"

    async def _fetch_markets(self) -> list:
        data = await fetch_json(
            "https://api.elections.kalshi.com/trade-api/v2/markets?limit=20",
            fixture="kalshi_markets.json",
        )
        return list((data.get("markets") or []) if isinstance(data, dict) else [])

    async def _fetch_count(self) -> int:
        return len(await self._fetch_markets()) if _enabled(self.collector_id) else 0

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            return self._mark_ok(await self._fetch_count(), "markets")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        n = 0
        for m in await self._fetch_markets():
            title = m.get("title") or m.get("ticker", "")
            mid = str(m.get("ticker") or title)[:120]
            yes_prob = None
            if m.get("yes_ask") is not None:
                try:
                    yes_prob = float(m["yes_ask"]) / 100.0
                except (TypeError, ValueError):
                    yes_prob = None
            await insert_odds_snapshot(
                session,
                venue="kalshi",
                market_id=mid,
                title=title,
                yes_prob=yes_prob,
                payload=m,
                captured_at=now,
            )
            n += 1
        return n


class BinanceLiquidationsCollector(NewsCollector):
    """Binance USD-M !forceOrder@arr (REST allForceOrders was retired)."""

    collector_id = "binance_liquidations"
    WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

    async def _read_ws_events(self, max_msgs: int = 25, timeout_s: float = 3.0) -> list[dict]:
        if use_fixtures():
            raw = load_fixture("binance_ws_force_orders.json")
            return raw if isinstance(raw, list) else []
        import websockets

        out: list[dict] = []
        async with websockets.connect(self.WS_URL, open_timeout=10) as ws:
            deadline = asyncio.get_event_loop().time() + timeout_s
            while len(out) < max_msgs and asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    if isinstance(data, dict) and data.get("e") == "forceOrder":
                        out.append(data)
                except asyncio.TimeoutError:
                    break
        return out

    def _aggregate(self, events: list[dict]) -> dict[str, dict]:
        buckets: dict[str, dict] = {}
        for ev in events:
            o = ev.get("o") or {}
            sym = o.get("s", "BTCUSDT")
            ts = int(o.get("T") or ev.get("E") or 0) // 60000 * 60000
            key = f"{sym}:{ts}"
            b = buckets.setdefault(key, {"long_usd": 0.0, "short_usd": 0.0, "count": 0, "minute_ms": ts, "symbol": sym})
            px = float(o.get("p") or 0)
            qty = float(o.get("q") or 0)
            usd = px * qty
            side = (o.get("S") or "").upper()
            if side == "SELL":
                b["long_usd"] += usd
            else:
                b["short_usd"] += usd
            b["count"] += 1
        return buckets

    async def _fetch_count(self) -> int:
        if not _enabled(self.collector_id):
            return 0
        return len(self._aggregate(await self._read_ws_events()))

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            n = await self._fetch_count()
            return self._mark_ok(n, f"{n} minute buckets")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        buckets = self._aggregate(await self._read_ws_events())
        n = 0
        from sqlalchemy import select

        for b in buckets.values():
            minute_at = datetime.fromtimestamp(b["minute_ms"] / 1000, tz=UTC).replace(tzinfo=None)
            row = await session.scalar(
                select(NewsLiquidationMinute).where(
                    NewsLiquidationMinute.symbol == b["symbol"],
                    NewsLiquidationMinute.minute_at == minute_at,
                )
            )
            if row is None:
                row = NewsLiquidationMinute(symbol=b["symbol"], minute_at=minute_at)
                session.add(row)
            row.long_usd = b["long_usd"]
            row.short_usd = b["short_usd"]
            row.count = b["count"]
            n += 1
        return n


class DefillamaCollector(NewsCollector):
    collector_id = "defillama"

    async def _fetch_rows(self) -> list:
        data = await fetch_json("https://api.llama.fi/v2/chains", fixture="defillama_chains.json")
        return data if isinstance(data, list) else []

    async def _fetch_count(self) -> int:
        return len(await self._fetch_rows()) if _enabled(self.collector_id) else 0

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            return self._mark_ok(await self._fetch_count(), "chains")
        except Exception as e:
            return self._mark_error(str(e))

    TOP_CHAINS = 25          # /v2/chains lists ~400 chains: every one every 5 min was ~115k rows a day
    EVERY = timedelta(hours=1)

    async def ingest(self, session) -> int:
        from sqlalchemy import func, select

        from storage.news_models import NewsDefiSnapshot

        now = datetime.now(UTC).replace(tzinfo=None)
        last = await session.scalar(select(func.max(NewsDefiSnapshot.captured_at)))
        if last is not None and now - last < self.EVERY:
            return 0
        rows = sorted(await self._fetch_rows(), key=lambda c: float(c.get("tvl") or 0), reverse=True)
        n = 0
        for ch in rows[: self.TOP_CHAINS]:
            tvl = float(ch.get("tvl") or 0)
            await insert_defi_snapshot(session, chain=str(ch.get("name") or ""), tvl_usd=tvl, captured_at=now)
            n += 1
        return n


class CrossAssetPricesCollector(NewsCollector):
    collector_id = "cross_asset_prices"

    async def _fetch_data(self) -> dict:
        data = await fetch_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
            fixture="cross_asset_prices.json",
        )
        return data if isinstance(data, dict) else {}

    async def _fetch_count(self) -> int:
        return len(await self._fetch_data()) if _enabled(self.collector_id) else 0

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            return self._mark_ok(await self._fetch_count(), "ticks")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        data = await self._fetch_data()
        n = 0
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            px = v.get("usd")
            if px is None:
                continue
            await insert_cross_asset_tick(
                session,
                symbol=k,
                price=float(px),
                change_24h_pct=float(v["usd_24h_change"]) if v.get("usd_24h_change") is not None else None,
                source_id=self.collector_id,
                captured_at=now,
            )
            n += 1
        return n


class RegionalRssCollector(NewsCollector):
    collector_id = "regional_rss"
    # Reuters world RSS was discontinued; BBC World is an official feed.
    FEED_URL = "https://feeds.bbci.co.uk/news/world/rss.xml"

    async def _fetch_items(self) -> list[dict]:
        xml = await fetch_text(self.FEED_URL, fixture="regional_rss.xml")
        return _parse_rss_items(xml)

    async def _fetch_count(self) -> int:
        return len(await self._fetch_items()) if _enabled(self.collector_id) else 0

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            return self._mark_ok(await self._fetch_count(), "headlines")
        except Exception as e:
            return self._mark_error(str(e))

    async def ingest(self, session) -> int:
        now = datetime.now(UTC).replace(tzinfo=None)
        n = 0
        for it in await self._fetch_items():
            await upsert_document(session, source_id=self.collector_id, title=it["title"], body=it.get("body") or "",
                                  url=it.get("url") or "", published_at=it.get("published") or now)
            n += 1
        return n


class Gdelt15mCollector(NewsCollector):
    collector_id = "gdelt_15m"

    async def _fetch_line(self) -> str:
        meta = await fetch_text(
            "http://data.gdeltproject.org/gdeltv2/lastupdate.txt",
            fixture="gdelt_lastupdate.txt",
        )
        return (meta.strip().splitlines() or [""])[-1]

    async def _fetch_count(self) -> int:
        return 1 if _enabled(self.collector_id) and await self._fetch_line() else 0

    async def poll(self) -> PollResult:
        if not _enabled(self.collector_id):
            return self._mark_ok(0, "disabled")
        try:
            line = await self._fetch_line()
            return self._mark_ok(1 if line else 0, line[:120])
        except Exception as e:
            return self._mark_error(str(e))

    async def _fetch_articles(self) -> list[dict]:
        if use_fixtures():
            raw = load_fixture("gdelt_articles.json")
            return raw if isinstance(raw, list) else list(raw.get("articles") or [])
        return []

    async def ingest(self, session) -> int:
        line = await self._fetch_line()
        n = 0
        if line:
            file_name = line.strip().split()[0]
            await upsert_gdelt_batch_pointer(session, file_name=file_name)
            n += 1
        now = datetime.now(UTC).replace(tzinfo=None)
        for art in await self._fetch_articles():
            await upsert_document(
                session,
                source_id="gdelt_15m:article",
                title=art.get("title") or "GDELT article",
                body=art.get("body") or "",
                url=art.get("url") or "",
                published_at=now,
                payload=art,
            )
            n += 1
        return n


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
