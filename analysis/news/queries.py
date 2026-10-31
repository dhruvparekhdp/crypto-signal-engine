"""Read models for monitor + moves (plan W3 — reuse event_monitor.ActiveEvent)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from analysis.event_monitor import ActiveEvent
from storage.news_models import NewsEvent


async def active_news_events(session: AsyncSession, now: datetime | None = None) -> list[ActiveEvent]:
    now = now or datetime.utcnow()
    rows = (
        await session.scalars(
            select(NewsEvent).where(NewsEvent.status == "open").order_by(NewsEvent.severity.desc())
        )
    ).all()
    out: list[ActiveEvent] = []
    for r in rows:
        out.append(
            ActiveEvent(
                id=r.id,
                title=r.title,
                category=r.event_type,
                level=int(r.severity_market or r.severity),
                first_seen=r.started_at,
                direction="mixed",
            )
        )
    return out
