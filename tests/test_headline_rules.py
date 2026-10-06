"""Headline scoring without a per-headline LLM call: FinBERT probabilities from our Space + rules."""
from unittest.mock import AsyncMock, patch

import pytest

from analysis.headline_rules import combine, event_type, symbol


@pytest.mark.parametrize("text,ev", [
    ("Fed cuts interest rate by 25 basis points", "rate_decision"),
    ("US CPI comes in hotter than expected", "inflation_data"),
    ("Exchange hacked, $200M drained from hot wallet", "hack"),
    ("Bitcoin ETF sees record inflows of $1.2B", "etf_flow"),
    ("SEC sues major crypto exchange", "regulation"),
    ("Bitcoin price prediction: could BTC reach $200k?", "noise"),
    ("Top 5 altcoins to buy now", "noise"),
    ("Solana network sees new developer activity", "crypto_other"),
])
def test_event_types(text, ev):
    assert event_type(text) == ev


def test_symbol_is_the_one_named_coin_or_all():
    assert symbol("Solana outage halts block production") == "solusdt"
    assert symbol("Bitcoin and Ethereum slide together") == "all"
    assert symbol("Bitcoin Cash hard fork scheduled") == "bchusdt"
    assert symbol("Oil jumps as tensions rise") == "all"


def test_combine_scores_named_events_and_silences_noise():
    r = combine({"positive": 0.9, "negative": 0.05, "neutral": 0.05}, "Bitcoin ETF sees record inflows")
    assert r == {"score": 0.85, "confidence": 0.9, "event_type": "etf_flow", "symbol": "btcusdt"}
    n = combine({"positive": 0.95, "negative": 0.0, "neutral": 0.05}, "Bitcoin price prediction: could BTC hit 200k")
    assert n["event_type"] == "noise" and n["score"] == 0.0 and n["confidence"] <= 0.1
    h = combine({"positive": 0.7, "negative": 0.2, "neutral": 0.1}, "Hacked exchange recovers part of stolen funds")
    assert h["event_type"] == "hack" and h["score"] <= -0.3
    m = combine({"positive": 0.1, "negative": 0.8, "neutral": 0.1}, "Fed hikes rates by 50 bps")
    assert m["symbol"] == "all" and m["score"] < 0


@pytest.mark.asyncio
async def test_space_client_sends_one_batch_with_the_saved_token(monkeypatch):
    from pydantic import SecretStr

    from collectors import hf_space
    from config.settings import settings
    monkeypatch.setattr(settings, "hf_base_url", "https://dhruvdp-dhruv-llm.hf.space")
    monkeypatch.setattr(settings, "hf_space_api_key", None)
    monkeypatch.setattr(settings, "hf_api_token", SecretStr("hf_token_x"))
    seen = {}

    class Resp:
        status_code = 200
        text = ""
        headers = {}
        def json(self):
            return {"results": [{"positive": 0.8, "negative": 0.1, "neutral": 0.1}] * 3}

    class Client:
        def __init__(self, timeout=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            seen.update(url=url, n=len(json["texts"]), auth=headers.get("Authorization"))
            return Resp()

    monkeypatch.setattr(hf_space.httpx, "AsyncClient", Client)
    rows = await hf_space.classify(["a", "b", "c"])
    assert len(rows) == 3 and seen == {"url": "https://dhruvdp-dhruv-llm.hf.space/v1/classify", "n": 3,
                                       "auth": "Bearer hf_token_x"}


async def _run_job(space_result, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from collectors import hermes, hf_space
    from config.settings import settings
    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    heads = [hermes.Headline(external_id=f"id{i}", headline=t, source="coindesk", url=f"https://x/{i}") for i, t in
             enumerate(["Bitcoin ETF sees record inflows", "Fed hikes rates by 50 bps"])]
    monkeypatch.setattr(settings, "news_sentiment_enabled", True)
    monkeypatch.setattr(hermes, "fetch_feeds", AsyncMock(return_value=SimpleNamespace(headlines=heads)))
    monkeypatch.setattr(hf_space, "configured", lambda: True)
    monkeypatch.setattr(hf_space, "classify", space_result)
    llm = AsyncMock(return_value=SimpleNamespace(data={"score": 0.1, "confidence": 0.5, "event_type": "noise",
                                                        "symbol": "all"}, served_by="groq/x", __bool__=lambda s: True))
    r = AppRunner()
    with patch("scheduler.runner.AsyncSessionFactory", sm), patch("storage.database.AsyncSessionFactory", sm), \
         patch("collectors.llm_client.ask_json", llm):
        await r._hermes_rss_job()
    from sqlalchemy import select

    from storage.models import NewsSentiment
    async with sm() as s:
        rows = (await s.execute(select(NewsSentiment))).scalars().all()
    return rows, llm


@pytest.mark.asyncio
async def test_job_scores_the_batch_on_the_space_without_llm_calls(monkeypatch):
    probs = AsyncMock(return_value=[{"positive": 0.9, "negative": 0.05, "neutral": 0.05},
                                    {"positive": 0.05, "negative": 0.9, "neutral": 0.05}])
    rows, llm = await _run_job(probs, monkeypatch)
    assert llm.await_count == 0 and probs.await_count == 1
    by = {r.headline: r for r in rows}
    assert by["Bitcoin ETF sees record inflows"].event_type == "etf_flow" and by["Fed hikes rates by 50 bps"].score < 0
    assert {r.model for r in rows} == {"hf/finbert+rules"}


@pytest.mark.asyncio
async def test_job_falls_back_to_the_llm_when_the_space_is_down(monkeypatch):
    rows, llm = await _run_job(AsyncMock(side_effect=RuntimeError("hf space returned 503")), monkeypatch)
    assert llm.await_count == 2 and len(rows) == 2
