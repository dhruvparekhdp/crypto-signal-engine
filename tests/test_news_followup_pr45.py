"""PR #45 follow-up: headline-only events, resolver query, close window, savepoints."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from analysis.news.paper_strategies import fed_reaction_side
from analysis.news.resolve_pipeline import (
    close_stale_open_events,
    resolve_pending_documents,
)
from analysis.news.store import upsert_document
from collectors.news.registry import setting_key_for
from collectors.news.sources import DefillamaCollector
from config import settings as settings_mod
from storage.database import Base
from storage.news_models import NewsDefiSnapshot, NewsDocument, NewsEvent, NewsEventStory


class TestFedNeutralPhrases(unittest.TestCase):
    def test_no_rush_to_cut_is_neutral(self):
        self.assertEqual(
            fed_reaction_side("central_bank", 4, title="Fed officials say no rush to cut rates"),
            0,
        )


class TestDataSourcesNoEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["NEWS_USE_FIXTURES"] = "1"
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        s = settings_mod.settings
        s.news_engine_enabled = True
        s.news_event_resolver_enabled = True
        setattr(s, setting_key_for("defillama"), True)

    async def asyncTearDown(self):
        os.environ.pop("NEWS_USE_FIXTURES", None)
        settings_mod.settings.news_engine_enabled = False
        await self.engine.dispose()

    async def test_tvl_ingest_does_not_create_news_event(self):
        async with self.Session() as session:
            n = await DefillamaCollector().ingest(session)
            self.assertGreater(n, 0)
            snaps = (await session.scalars(select(NewsDefiSnapshot))).all()
            self.assertGreater(len(snaps), 0)
            docs = (await session.scalars(select(NewsDocument))).all()
            self.assertEqual(len(docs), 0)
            created = await resolve_pending_documents(session)
            self.assertEqual(created, 0)
            events = (await session.scalars(select(NewsEvent))).all()
            self.assertEqual(len(events), 0)
            await session.commit()


class TestResolverUnlinkedOnly(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.base = datetime(2026, 10, 30, 12, 0)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_hundred_pending_docs_processes_unlinked_not_newest_forty(self):
        async with self.Session() as session:
            for i in range(100):
                await upsert_document(
                    session,
                    source_id="regional_rss",
                    title=f"Headline number {i}",
                    published_at=self.base + timedelta(minutes=i),
                )
            await session.commit()

        async with self.Session() as session:
            docs = (await session.scalars(select(NewsDocument).order_by(NewsDocument.id))).all()
            # Link the 60 newest (highest i) so 40 oldest remain unlinked
            for doc in docs[-60:]:
                ev = NewsEvent(
                    event_key=f"prelinked-{doc.id}",
                    title=doc.title,
                    event_type="other",
                    severity=1,
                    status="open",
                    started_at=doc.published_at,
                )
                session.add(ev)
                await session.flush()
                session.add(NewsEventStory(event_id=ev.id, document_id=doc.id, is_followup=False))
            await session.commit()

        async with self.Session() as session:
            created = await resolve_pending_documents(session, limit=40)
            self.assertEqual(created, 40)
            unlinked = await session.scalar(
                select(func.count())
                .select_from(NewsDocument)
                .where(
                    NewsDocument.id.not_in(select(NewsEventStory.document_id)),
                    NewsDocument.source_id == "regional_rss",
                )
            )
            self.assertEqual(unlinked, 0)
            await session.commit()


class TestCloseEventsMemoryWindow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_l1_resolves_after_six_hours_without_new_story(self):
        started = datetime(2026, 10, 30, 10, 0)
        now = started + timedelta(hours=7)
        async with self.Session() as session:
            ev = NewsEvent(
                event_key="close-me",
                title="Background comment",
                event_type="other",
                severity=1,
                status="open",
                started_at=started,
            )
            session.add(ev)
            await session.commit()

        async with self.Session() as session:
            n = await close_stale_open_events(session, now=now)
            self.assertEqual(n, 1)
            ev = await session.scalar(select(NewsEvent).where(NewsEvent.event_key == "close-me"))
            self.assertEqual(ev.status, "resolved")
            self.assertIsNotNone(ev.resolved_at)

    async def test_recent_story_keeps_event_open(self):
        started = datetime(2026, 10, 30, 10, 0)
        now = started + timedelta(hours=7)
        async with self.Session() as session:
            doc = await upsert_document(
                session,
                source_id="regional_rss",
                title="Follow-up",
                published_at=started + timedelta(hours=6, minutes=30),
            )
            ev = NewsEvent(
                event_key="stay-open",
                title="Background comment",
                event_type="other",
                severity=1,
                status="open",
                started_at=started,
            )
            session.add(ev)
            await session.flush()
            session.add(
                NewsEventStory(
                    event_id=ev.id,
                    document_id=doc.id,
                    is_followup=True,
                    created_at=started + timedelta(hours=6, minutes=30),
                )
            )
            await session.commit()

        async with self.Session() as session:
            n = await close_stale_open_events(session, now=now)
            self.assertEqual(n, 0)
            ev = await session.scalar(select(NewsEvent).where(NewsEvent.event_key == "stay-open"))
            self.assertEqual(ev.status, "open")


class TestCollectorSavepoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["NEWS_USE_FIXTURES"] = "1"
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        os.environ.pop("NEWS_USE_FIXTURES", None)
        await self.engine.dispose()

    async def test_one_collector_failure_does_not_roll_back_other(self):
        from collectors.news.base import NewsCollector
        from collectors.news.sources import RegionalRssCollector

        class BoomCollector(NewsCollector):
            collector_id = "boom_test"

            async def ingest(self, session) -> int:
                raise RuntimeError("boom")

            async def _fetch_count(self) -> int:
                return 0

        async with self.Session() as session:
            ok = await RegionalRssCollector().poll_and_store(session)
            self.assertGreater(ok.items, 0)
            bad = await BoomCollector().poll_and_store(session)
            self.assertEqual(bad.items, 0)
            self.assertEqual(bad.message, "boom")
            # Regional RSS stored rows despite boom collector
            n = await session.scalar(select(func.count()).select_from(NewsDocument))
            self.assertGreater(n, 0)
            await session.commit()
