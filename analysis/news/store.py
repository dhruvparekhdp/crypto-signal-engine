"""Persist collector output (news_documents, news_calendar_rows)."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from analysis.news.normalize import clean_title, document_external_id
from storage.news_models import (
    NewsCalendarRow,
    NewsCrossAssetTick,
    NewsDefiSnapshot,
    NewsDocument,
    NewsGdeltBatch,
    NewsOddsSnapshot,
)


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
        if published_at and (row.published_at is None or published_at < row.published_at):
            row.published_at = published_at          # a source's own (earlier) time replaces a fetch-time stamp
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


async def insert_odds_snapshot(
    session: AsyncSession,
    *,
    venue: str,
    market_id: str,
    title: str,
    yes_prob: float | None,
    volume_usd: float | None = None,
    payload: dict | None = None,
    captured_at: datetime | None = None,
) -> NewsOddsSnapshot:
    from storage.models import _now_utc

    row = NewsOddsSnapshot(
        venue=venue,
        market_id=market_id,
        title=title,
        yes_prob=yes_prob,
        volume_usd=volume_usd,
        payload_json=json.dumps(payload or {}),
        captured_at=captured_at or _now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def insert_defi_snapshot(
    session: AsyncSession,
    *,
    chain: str,
    tvl_usd: float,
    stablecoin_usd: float | None = None,
    captured_at: datetime | None = None,
) -> NewsDefiSnapshot:
    from storage.models import _now_utc

    row = NewsDefiSnapshot(
        chain=chain,
        tvl_usd=tvl_usd,
        stablecoin_usd=stablecoin_usd,
        captured_at=captured_at or _now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def insert_cross_asset_tick(
    session: AsyncSession,
    *,
    symbol: str,
    price: float,
    change_24h_pct: float | None = None,
    source_id: str = "",
    captured_at: datetime | None = None,
) -> NewsCrossAssetTick:
    from storage.models import _now_utc

    row = NewsCrossAssetTick(
        symbol=symbol,
        price=price,
        change_24h_pct=change_24h_pct,
        source_id=source_id,
        captured_at=captured_at or _now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def insert_fred_observation(
    session: AsyncSession,
    *,
    series_id: str,
    observation_date: str,
    value: str,
    payload: dict | None = None,
    captured_at: datetime | None = None,
) -> NewsCrossAssetTick:
    """FRED points live in cross-asset ticks (series as symbol) — not headline documents."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        price = 0.0
    return await insert_cross_asset_tick(
        session,
        symbol=f"FRED:{series_id}:{observation_date}",
        price=price,
        source_id="us_treasury_fred",
        captured_at=captured_at,
    )


async def upsert_gdelt_batch_pointer(session: AsyncSession, *, file_name: str) -> NewsGdeltBatch:
    from storage.models import _now_utc

    row = await session.scalar(select(NewsGdeltBatch).where(NewsGdeltBatch.file_name == file_name))
    if row is None:
        row = NewsGdeltBatch(file_name=file_name, status="seen")
        session.add(row)
    else:
        row.status = "seen"
        row.processed_at = _now_utc()
    await session.flush()
    return row
