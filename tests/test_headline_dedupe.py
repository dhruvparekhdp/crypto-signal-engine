"""Same story from many feeds (owner, 18 Oct 2026, AI plan step 5)."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from analysis.headline_dedupe import RecentHeadlines, mark_duplicates, normalise

T0 = datetime(2026, 10, 18, 9, 0)


def H(i, text, minutes=0, tz=False):
    when = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(external_id=f"id{i}", headline=text, published_at=when.replace(tzinfo=UTC) if tz else when,
                           duplicate_of=None)


def test_the_same_words_with_different_punctuation_are_one_story_without_any_vectors():
    assert normalise("Bitcoin ETF sees $500M inflows!") == normalise("bitcoin etf sees 500m inflows")
    hs = [H(1, "Bitcoin ETF sees $500M inflows!"), H(2, "bitcoin ETF sees 500M inflows")]
    assert mark_duplicates(hs, None, RecentHeadlines(), T0) == 1
    assert (hs[0].duplicate_of, hs[1].duplicate_of) == ("", "id1")


def test_close_meaning_is_a_duplicate_and_a_different_story_is_not():
    hs = [H(1, "Fed holds rates at 4.25%"), H(2, "Federal Reserve keeps rates unchanged"), H(3, "Hacker drains $40M")]
    vecs = [[1.0, 0.0, 0.0], [0.95, 0.31, 0.0], [0.0, 0.0, 1.0]]          # cos(1,2) = 0.95, cos(1,3) = 0
    assert mark_duplicates(hs, vecs, RecentHeadlines(threshold=0.9), T0) == 1
    assert [h.duplicate_of for h in hs] == ["", "id1", ""]


def test_below_the_threshold_both_stay():
    hs = [H(1, "ETH upgrade date set"), H(2, "ETH devs discuss upgrade")]
    assert mark_duplicates(hs, [[1.0, 0.0], [0.8, 0.6]], RecentHeadlines(threshold=0.9), T0) == 0


def test_a_repeat_after_the_window_is_news_again():
    recent = RecentHeadlines(window_hours=12)
    mark_duplicates([H(1, "Binance halts withdrawals")], None, recent, T0)
    later = [H(2, "Binance halts withdrawals", minutes=13 * 60)]
    assert mark_duplicates(later, None, recent, T0 + timedelta(hours=13)) == 0 and later[0].duplicate_of == ""


def test_feed_times_with_a_zone_mix_with_database_times():
    recent = RecentHeadlines()
    recent.add("db1", "SEC approves spot SOL ETF", None, T0)                # naive UTC, as stored
    hs = [H(2, "SEC approves spot SOL ETF", minutes=30, tz=True)]
    assert mark_duplicates(hs, None, recent, T0.replace(tzinfo=UTC) + timedelta(minutes=30)) == 1
    assert hs[0].duplicate_of == "db1"


@pytest.mark.asyncio
async def test_readers_of_the_news_skip_duplicates_but_can_ask_for_them():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from storage.models import Base
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sm() as s:
        await Repository(s).ingest_news_sentiment([
            {"external_id": "a", "headline": "Fed holds", "score": 0.1, "published_at": now.isoformat()},
            {"external_id": "b", "headline": "Fed keeps rates", "score": 0.1, "published_at": now.isoformat(),
             "duplicate_of": "a"}])
    async with sm() as s:
        repo = Repository(s)
        assert [r.external_id for r in await repo.news_sentiment_since(6)] == ["a"]
        assert {r.external_id for r in await repo.news_sentiment_since(6, include_duplicates=True)} == {"a", "b"}
        assert [r.external_id for r in await repo.recent_news_sentiment("btcusdt", 6)] == ["a"]


@pytest.mark.asyncio
async def test_the_rss_job_stores_repeats_without_spending_ai_on_them(monkeypatch):
    """Space down: exact-text fallback. Three headlines, two the same story: two LLM scoring calls, three rows."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from collectors import hermes, hf_space
    from config.settings import settings
    from scheduler.runner import AppRunner
    from storage.models import Base, NewsSentiment
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    heads = [hermes.Headline(external_id=f"id{i}", headline=t, source=src, url=f"https://x/{i}", published_at=now)
             for i, (t, src) in enumerate([("Bitcoin ETF sees record inflows", "coindesk"),
                                            ("Bitcoin ETF sees record inflows!", "cointelegraph"),
                                            ("Fed hikes rates by 50 bps", "cnbc")])]
    monkeypatch.setattr(settings, "news_sentiment_enabled", True)
    monkeypatch.setattr(settings, "headline_dedupe_enabled", True)
    monkeypatch.setattr(hermes, "fetch_feeds", AsyncMock(return_value=SimpleNamespace(headlines=heads)))
    monkeypatch.setattr(hf_space, "configured", lambda: False)
    llm = AsyncMock(return_value=SimpleNamespace(data={"score": 0.3, "confidence": 0.6, "event_type": "etf_flow",
                                                        "symbol": "all"}, served_by="gemini/x", __bool__=lambda s: True))
    r = AppRunner()
    with patch("scheduler.runner.AsyncSessionFactory", sm), patch("storage.database.AsyncSessionFactory", sm), \
         patch("collectors.llm_client.ask_json", llm):
        await r._hermes_rss_job()
    async with sm() as s:
        rows = {x.external_id: x for x in (await s.execute(select(NewsSentiment))).scalars().all()}
    assert llm.await_count == 2                                          # the repeat was never sent to the AI
    assert set(rows) == {"id0", "id1", "id2"} and rows["id1"].duplicate_of == "id0"
    assert rows["id0"].duplicate_of == "" and rows["id2"].duplicate_of == ""
