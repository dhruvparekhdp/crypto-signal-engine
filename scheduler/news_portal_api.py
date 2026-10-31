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
