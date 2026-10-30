"""Persist collector output (news_documents, news_calendar_rows)."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from analysis.news.normalize import clean_title, document_external_id
from storage.news_models import NewsCalendarRow, NewsDocument


async def upsert_document(session: AsyncSession, *, source_id: str, title: str, body: str = "",
                          url: str = "", published_at: datetime, payload: dict | None = None) -> NewsDocument:
    ext = document_external_id(source_id, title, url)
    row = await session.scalar(
        select(NewsDocument).where(NewsDocument.source_id == source_id, NewsDocument.external_id == ext)
    )
    if row is None:
        row = NewsDocument(
            source_id=source_id,
            external_id=ext,
            title=clean_title(title),
            body=body or "",
            url=url or "",
            published_at=published_at,
            payload_json=json.dumps(payload or {}),
        )
        session.add(row)
    else:
        row.title = clean_title(title)
        row.body = body or row.body
        row.payload_json = json.dumps(payload or {})
    await session.flush()
    return row


async def upsert_calendar_row(session: AsyncSession, *, source_id: str, external_id: str, title: str,
                              country: str, scheduled_at: datetime, consensus: str = "", previous: str = "",
                              actual: str = "", severity: int = 2, payload: dict | None = None) -> NewsCalendarRow:
    row = await session.scalar(
        select(NewsCalendarRow).where(NewsCalendarRow.source_id == source_id, NewsCalendarRow.external_id == external_id)
    )
    if row is None:
        row = NewsCalendarRow(
            source_id=source_id,
            external_id=external_id,
            title=title,
            country=country,
            scheduled_at=scheduled_at,
            consensus=consensus,
            previous=previous,
            actual=actual,
            severity=severity,
            payload_json=json.dumps(payload or {}),
        )
        session.add(row)
    else:
        row.consensus = consensus or row.consensus
        row.actual = actual or row.actual
        row.payload_json = json.dumps(payload or {})
    await session.flush()
    return row
