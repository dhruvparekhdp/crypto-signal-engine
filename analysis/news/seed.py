"""Seed institutions into news_institutions (idempotent)."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from analysis.news.institutions import load_institutions
from storage.news_models import NewsInstitution


async def seed_institutions(session: AsyncSession) -> int:
    n = 0
    for row in load_institutions():
        code = row["code"]
        existing = await session.scalar(select(NewsInstitution).where(NewsInstitution.code == code))
        if existing:
            continue
        session.add(
            NewsInstitution(
                code=code,
                name=row["name"],
                country=row.get("country", ""),
                kind=row.get("kind", ""),
                aliases_json=json.dumps(row.get("aliases") or []),
            )
        )
        n += 1
    if n:
        await session.commit()
    return n
