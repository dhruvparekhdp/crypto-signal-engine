"""Our own headline triage (owner, 18 Oct 2026, AI plan step 6)."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from analysis import headline_triage as H

# a tiny 2-dimensional model: x[0] large = relevant; event by direction of x; direction by x[1]
TINY = {"dim": 2, "threshold": 0.35,
        "relevance": {"classes": [False, True], "coef": [[6.0, 0.0]], "intercept": [-3.0]},
        "event": {"classes": ["hack", "inflation_data", "noise", "regulation"],
                  "coef": [[0.0, -6.0], [0.0, 6.0], [-6.0, 0.0], [3.0, 0.0]], "intercept": [0.0, 0.0, 0.0, 0.0]},
        "direction": {"classes": [-1, 0, 1], "coef": [[0.0, -6.0], [0.0, 0.0], [0.0, 6.0]], "intercept": [0.0, 0.0, 0.0]}}


def test_the_shipped_model_loads_and_beat_finbert_on_both_mistakes():
    H.load.cache_clear()
    m = H.load()
    assert m is not None and m["dim"] == 384 and m["threshold"] == 0.35 and "noise" in m["event"]["classes"]
    ev = m["rolling_eval"]
    assert ev["noise_through"] < ev["base_noise_through"] and ev["news_dropped"] < ev["base_news_dropped"]


def test_below_the_cut_off_is_noise_with_no_score():
    r = H.classify([-1.0, 0.0], "Top 5 meme coins to watch", TINY)          # unit length: direction counts
    assert (r["relevant"], r["event_type"], r["score"]) == (False, "noise", 0.0) and r["confidence"] <= 0.1


def test_an_unconfirmed_high_impact_headline_cannot_pause_trading():
    from analysis.event_calendar import HIGH_IMPACT_NEWS_CONFIDENCE, news_events
    r = H.classify([1.0, 1.0], "US CPI rises 0.6% in September, hotter than expected", TINY)
    assert r["event_type"] == "inflation_data" and r["symbol"] == "all"
    assert r["confidence"] < HIGH_IMPACT_NEWS_CONFIDENCE                 # held under the pause threshold
    now = datetime(2026, 10, 18, 12)
    row = SimpleNamespace(event_type=r["event_type"], confidence=r["confidence"], published_at=now - timedelta(minutes=5),
                          headline="US CPI rises 0.6%")
    assert news_events([row], now, H.HIGH_IMPACT) == ()                  # only the LLM check can confirm it


def test_a_hack_is_never_good_news():
    r = H.classify([1.0, -1.0], "Exchange recovers funds after $40M hack", TINY)
    assert r["event_type"] == "hack" and r["score"] <= -0.3


def test_no_model_or_a_wrong_vector_means_no_answer():
    assert H.classify([1.0, 0.0, 0.0], "x", TINY) is None and H.classify([], "x", TINY) is None


async def _run_job(monkeypatch, vectors):
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
    heads = [hermes.Headline(external_id=f"id{i}", headline=t, source="coindesk", url=f"https://x/{i}")
             for i, t in enumerate(["US CPI rises 0.6% in September", "Top 5 meme coins to watch"])]
    for k, v in {"news_sentiment_enabled": True, "headline_dedupe_enabled": True, "headline_triage_enabled": True}.items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(hermes, "fetch_feeds", AsyncMock(return_value=SimpleNamespace(headlines=heads)))
    monkeypatch.setattr(hf_space, "configured", lambda: True)
    monkeypatch.setattr(hf_space, "embed", AsyncMock(return_value=vectors))
    classify = AsyncMock(return_value=[{"positive": 0.1, "negative": 0.8, "neutral": 0.1}] * 2)
    monkeypatch.setattr(hf_space, "classify", classify)
    monkeypatch.setattr(H, "load", lambda path=None: TINY)
    r = AppRunner()
    r._llm_check_headlines = AsyncMock()
    with patch("scheduler.runner.AsyncSessionFactory", sm), patch("storage.database.AsyncSessionFactory", sm):
        await r._hermes_rss_job()
    async with sm() as s:
        rows = {x.external_id: x for x in (await s.execute(select(NewsSentiment))).scalars().all()}
    return rows, classify, r._llm_check_headlines


@pytest.mark.asyncio
async def test_the_rss_job_scores_with_our_model_when_every_headline_has_a_vector(monkeypatch):
    rows, classify, check = await _run_job(monkeypatch, [[1.0, 1.0], [-1.0, 0.0]])
    assert not classify.await_count                                       # no FinBERT call
    assert rows["id0"].event_type == "inflation_data" and rows["id0"].model.startswith("triage-v1")
    assert rows["id1"].event_type == "noise" and rows["id1"].score == 0.0
    sent = check.await_args.args[0]
    assert [x["external_id"] for x in sent] == ["id0", "id1"]            # the LLM check still sees the batch


@pytest.mark.asyncio
async def test_without_vectors_finbert_scores_as_before(monkeypatch):
    rows, classify, _ = await _run_job(monkeypatch, [])                   # the Space could not embed
    assert classify.await_count == 1 and rows["id0"].model == "hf/finbert+rules"
