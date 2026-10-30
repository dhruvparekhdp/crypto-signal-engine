"""
News engine scheduled cycle (plan W14): collectors → DB → resolver. Default-off via settings.
"""
from __future__ import annotations

import asyncio

import structlog

from config.settings import settings
from collectors.news.registry import all_collectors, setting_key_for
from storage.database import AsyncSessionFactory
from storage.news_models import NewsCollectorState

log = structlog.get_logger(__name__)


async def run_news_engine_cycle() -> dict:
    if not settings.news_engine_enabled:
        return {"skipped": True}
    from analysis.news.resolve_pipeline import resolve_pending_documents
    from collectors.news.base import NewsCollector

    stats = {"collectors": 0, "items": 0, "events_created": 0}
    async with AsyncSessionFactory() as session:
        for collector in all_collectors():
            if not getattr(settings, setting_key_for(collector.collector_id), False):
                continue
            stats["collectors"] += 1
            if isinstance(collector, NewsCollector):
                result = await collector.poll_and_store(session)
                stats["items"] += result.items
                await _persist_health(session, collector)
        if settings.news_event_resolver_enabled:
            from analysis.news.resolve_pipeline import close_stale_open_events, resolve_calendar_releases

            stats["events_created"] = await resolve_pending_documents(session)
            stats["events_created"] += await resolve_calendar_releases(session)
            stats["events_closed"] = await close_stale_open_events(session)
        await session.commit()
    log.info("news_engine_cycle", **stats)
    return stats


async def _persist_health(session, collector) -> None:
    h = collector.health
    row = await session.get(NewsCollectorState, collector.collector_id)
    if row is None:
        row = NewsCollectorState(collector_id=collector.collector_id)
        session.add(row)
    row.enabled = True
    row.status = h.status
    row.message = h.message
    row.items_last_run = h.items_last_run
    row.last_ok_at = h.last_ok_at
    row.last_error_at = h.last_error_at


