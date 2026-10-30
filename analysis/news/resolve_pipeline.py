"""Turn headline documents and calendar rows into news_events (plan W7)."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from analysis.news.classifier import classify_rule_based
from analysis.news.memory import memory_window_for_severity
from analysis.news.resolver import event_key_for
from analysis.news.source_policy import document_eligible_for_event
from storage.news_models import NewsCalendarRow, NewsDocument, NewsEvent, NewsEventStory


async def resolve_pending_documents(session: AsyncSession, *, limit: int = 40) -> int:
    """Create events only for headline documents with no NewsEventStory link."""
    from sqlalchemy import or_

    linked_doc_ids = select(NewsEventStory.document_id)
    headline_filter = or_(
        NewsDocument.source_id == "regional_rss",
        NewsDocument.source_id.like("central_bank_rss:%"),
        NewsDocument.source_id.like("gdelt_15m:article%"),
    )
    docs = (
        await session.scalars(
            select(NewsDocument)
            .where(NewsDocument.duplicate_of == "")
            .where(NewsDocument.id.not_in(linked_doc_ids))
            .where(headline_filter)
            .order_by(NewsDocument.published_at.asc())
            .limit(limit)
        )
    ).all()
    created = 0
    for doc in docs:
        if not document_eligible_for_event(doc.source_id):
            continue
        created += await _link_document_to_event(session, doc)
    return created


async def _link_document_to_event(session: AsyncSession, doc: NewsDocument) -> int:
    meta = classify_rule_based(doc.title, doc.body)
    key = event_key_for(doc.title, meta["event_type"], doc.published_at)
    ev = await session.scalar(select(NewsEvent).where(NewsEvent.event_key == key))
    created = 0
    if ev is None:
        ev = NewsEvent(
            event_key=key,
            title=doc.title,
            event_type=meta["event_type"],
            severity=meta["severity"],
            countries_json=json.dumps(meta["countries"]),
            institutions_json=json.dumps(meta["institutions"]),
            status="open",
            started_at=doc.published_at,
        )
        session.add(ev)
        await session.flush()
        created = 1
    session.add(NewsEventStory(event_id=ev.id, document_id=doc.id, is_followup=False))
    return created


async def resolve_calendar_releases(session: AsyncSession, *, limit: int = 40) -> int:
    """Calendar rows with an actual print become canonical macro events."""
    rows = (
        await session.scalars(
            select(NewsCalendarRow)
            .where(NewsCalendarRow.event_id.is_(None))
            .where(NewsCalendarRow.actual != "")
            .order_by(NewsCalendarRow.scheduled_at.asc())
            .limit(limit)
        )
    ).all()
    created = 0
    for row in rows:
        title = f"{row.country} {row.title} actual {row.actual}".strip()
        meta = classify_rule_based(title, f"consensus {row.consensus} previous {row.previous}")
        key = event_key_for(title, meta["event_type"], row.scheduled_at)
        ev = await session.scalar(select(NewsEvent).where(NewsEvent.event_key == key))
        if ev is None:
            ev = NewsEvent(
                event_key=key,
                title=title,
                event_type=meta["event_type"],
                severity=max(meta["severity"], row.severity),
                countries_json=json.dumps(meta["countries"] or ([row.country] if row.country else [])),
                institutions_json=json.dumps(meta["institutions"]),
                status="open",
                started_at=row.scheduled_at,
            )
            session.add(ev)
            await session.flush()
            created += 1
        row.event_id = ev.id
    return created


async def close_stale_open_events(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Mark open events resolved after their memory window with no new linked story."""
    now = now or datetime.utcnow()
    open_events = (await session.scalars(select(NewsEvent).where(NewsEvent.status == "open"))).all()
    closed = 0
    for ev in open_events:
        last_story = await session.scalar(
            select(func.max(NewsEventStory.created_at)).where(NewsEventStory.event_id == ev.id)
        )
        anchor = last_story or ev.started_at
        if now - anchor < memory_window_for_severity(ev.severity):
            continue
        ev.status = "resolved"
        ev.resolved_at = now
        closed += 1
    return closed
