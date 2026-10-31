"""Portal JSON for Command > World / Events / Calendar (plan W12)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from aiohttp import web
from sqlalchemy import select

from config.settings import settings
from storage.database import AsyncSessionFactory
from storage.news_models import NewsCalendarRow, NewsEvent, NewsSentimentIndex


def _iso(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat() + "Z"


async def api_portal_news(request: web.Request) -> web.Response:
    if not settings.news_engine_enabled or not settings.news_portal_widgets_enabled:
        return web.Response(
            text=json.dumps({"enabled": False, "message": "News engine portal widgets are off."}),
            content_type="application/json",
        )
    async with AsyncSessionFactory() as session:
        events = (await session.scalars(select(NewsEvent).order_by(NewsEvent.started_at.desc()).limit(40))).all()
        cal = (await session.scalars(select(NewsCalendarRow).order_by(NewsCalendarRow.scheduled_at.desc()).limit(30))).all()
        idx = (await session.scalars(select(NewsSentimentIndex).order_by(NewsSentimentIndex.captured_at.desc()).limit(10))).all()
    return web.Response(
        text=json.dumps({
            "enabled": True,
            "events": [{
                "id": e.id, "title": e.title, "type": e.event_type,
                "severity": e.severity, "severity_market": e.severity_market,
                "status": e.status, "at": _iso(e.started_at),
            } for e in events],
            "calendar": [{
                "title": c.title, "country": c.country, "at": _iso(c.scheduled_at),
                "consensus": c.consensus, "actual": c.actual, "severity": c.severity,
            } for c in cal],
            "indices": [{
                "scope": i.scope, "key": i.scope_key, "score": i.score,
                "confidence": i.confidence, "at": _iso(i.captured_at),
            } for i in idx],
            "generated_at": _iso(datetime.now(UTC)),
        }),
        content_type="application/json",
    )


# ── /world (owner 31 Oct 2026: "a,b and c all design with switch, and deploy") ─────────────────────────────────────────
# The 3D World page reads one JSON: open and recent events with their countries and source, a per-country "news heat",
# and the ticker (prediction-market odds, DeFi total, liquidations, cross-asset prices). Labels only, never trades.

SOURCE_COUNTRY = {"central_bank_rss:fed": "US", "central_bank_rss:ecb": "EU"}
SOURCE_LABEL = {"central_bank_rss:fed": "Federal Reserve press", "central_bank_rss:ecb": "ECB press",
                "regional_rss": "BBC World", "gdelt_15m:article": "GDELT"}


def event_countries(countries_json: str, source_id: str) -> list[str]:
    """The event's countries; an official press item without one belongs to its bank's country."""
    try:
        out = [str(c).upper() for c in json.loads(countries_json or "[]") if c]
    except (TypeError, ValueError):
        out = []
    if not out and source_id in SOURCE_COUNTRY:
        out = [SOURCE_COUNTRY[source_id]]
    return out


def country_heat(events: list[dict]) -> dict[str, float]:
    """0..1 per country: the severity of its open events (L1 = 1 point ... L5 = 5), scaled to the hottest country.
    How much is happening there, not whether it is good or bad (the engine has no direction for most items)."""
    pts: dict[str, float] = {}
    for e in events:
        if e.get("status") != "open":
            continue
        for c in e.get("countries") or []:
            pts[c] = pts.get(c, 0.0) + float(e.get("level") or 1)
    top = max(pts.values(), default=0.0)
    return {c: round(v / top, 3) for c, v in pts.items()} if top else {}


async def api_portal_world(request: web.Request) -> web.Response:
    if not settings.news_engine_enabled or not settings.news_portal_widgets_enabled:
        return web.Response(text=json.dumps({"enabled": False, "message": "The news engine is off (Settings > Advanced)."}),
                            content_type="application/json")
    from datetime import timedelta

    from sqlalchemy import func

    from storage.news_models import (NewsCrossAssetTick, NewsDefiSnapshot, NewsDocument, NewsEventStory,
                                     NewsLiquidationMinute, NewsOddsSnapshot)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with AsyncSessionFactory() as session:
        evs = (await session.scalars(
            select(NewsEvent).where((NewsEvent.status == "open") | (NewsEvent.started_at >= now - timedelta(hours=48)))
            .order_by(NewsEvent.started_at.desc()).limit(80))).all()
        firsts = {}
        if evs:
            rows = (await session.execute(
                select(NewsEventStory.event_id, NewsDocument.source_id, NewsDocument.url)
                .join(NewsDocument, NewsDocument.id == NewsEventStory.document_id)
                .where(NewsEventStory.event_id.in_([e.id for e in evs])))).all()
            for eid, sid, url in rows:
                firsts.setdefault(eid, (sid, url))
        out = []
        for e in evs:
            sid, url = firsts.get(e.id, ("", ""))
            out.append({"id": e.id, "title": e.title, "type": e.event_type, "level": int(e.severity_market or e.severity),
                        "status": e.status, "at": _iso(e.started_at), "source": SOURCE_LABEL.get(sid, sid or "news"),
                        "url": url if str(url).startswith("http") else "", "countries": event_countries(e.countries_json, sid)})
        out.sort(key=lambda x: x["at"], reverse=True)                     # newest first ...
        out.sort(key=lambda x: (x["status"] != "open", -x["level"]))      # ... within open-first, highest level
        last_odds = await session.scalar(select(func.max(NewsOddsSnapshot.captured_at)))
        odds = (await session.scalars(select(NewsOddsSnapshot).where(NewsOddsSnapshot.captured_at == last_odds)
                                      .limit(40))).all() if last_odds else []
        last_defi = await session.scalar(select(func.max(NewsDefiSnapshot.captured_at)))
        defi = await session.scalar(select(func.sum(NewsDefiSnapshot.tvl_usd))
                                    .where(NewsDefiSnapshot.captured_at == last_defi)) if last_defi else None
        liq = (await session.execute(select(func.sum(NewsLiquidationMinute.long_usd), func.sum(NewsLiquidationMinute.short_usd))
                                     .where(NewsLiquidationMinute.minute_at >= now - timedelta(hours=1)))).one()
        last_px = await session.scalar(select(func.max(NewsCrossAssetTick.captured_at)))
        px = (await session.scalars(select(NewsCrossAssetTick).where(NewsCrossAssetTick.captured_at == last_px))).all() \
            if last_px else []
    ticker = []
    for o in sorted((o for o in odds if o.yes_prob is not None and 0 < o.yes_prob < 1), key=lambda o: -(o.volume_usd or 0))[:6]:
        ticker.append({"k": o.venue.upper(), "v": f"{o.title[:70]} {round(o.yes_prob * 100)}%"})
    if defi:
        ticker.append({"k": "DEFI TVL", "v": f"${defi / 1e9:,.1f}B (top 25 chains)"})
    if liq and (liq[0] or liq[1]):
        ticker.append({"k": "LIQUIDATIONS 1H", "v": f"longs ${(liq[0] or 0) / 1e6:,.1f}M · shorts ${(liq[1] or 0) / 1e6:,.1f}M"})
    for p in px:
        ticker.append({"k": p.symbol.upper(), "v": f"${p.price:,.0f}"})
    body = {"enabled": True, "events": out, "heat": country_heat(out), "ticker": ticker,
            "open": sum(1 for e in out if e["status"] == "open"), "generated_at": _iso(datetime.now(UTC))}
    return web.Response(text=json.dumps(body), content_type="application/json")
