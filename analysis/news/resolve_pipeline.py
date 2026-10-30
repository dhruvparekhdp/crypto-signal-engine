"""Turn news_documents into news_events (resolver job, plan W7)."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from analysis.news.classifier import classify_rule_based
from analysis.news.resolver import event_key_for
from storage.news_models import NewsDocument, NewsEvent, NewsEventStory


async def resolve_pending_documents(session: AsyncSession, *, limit: int = 40) -> int:
    """Create canonical events for documents not yet linked; skip duplicates."""
    docs = (
        await session.scalars(
            select(NewsDocument)
            .where(NewsDocument.duplicate_of == "")
            .order_by(NewsDocument.published_at.desc())
            .limit(limit)
        )
    ).all()
    created = 0
    for doc in docs:
        linked = await session.scalar(select(NewsEventStory.id).where(NewsEventStory.document_id == doc.id))
        if linked:
            continue
        meta = classify_rule_based(doc.title, doc.body)
        key = event_key_for(doc.title, meta["event_type"], doc.published_at)
        ev = await session.scalar(select(NewsEvent).where(NewsEvent.event_key == key))
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
            created += 1
        session.add(NewsEventStory(event_id=ev.id, document_id=doc.id, is_followup=False))
    return created
