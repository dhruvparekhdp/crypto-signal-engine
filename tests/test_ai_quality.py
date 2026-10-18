"""AI quality ledger and the System > AI tab (owner, 18 Oct 2026, AI plan step 4)."""
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from collectors import ai_ledger
from collectors.llm_client import ask_json


def _rows(root):
    out = []
    for p in sorted(root.glob("*.jsonl")):
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return out


def test_summary_counts_answers_failures_rejections_and_speed(tmp_path):
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    note = lambda *a, **k: ai_ledger.note(*a, root=tmp_path, now=k.pop("at", now), **k)   # noqa: E731
    note("briefing", "ok", "gemini", "flash-lite", 4000)
    note("briefing", "budget_skip", "groq", "gpt-oss-120b", detail="daily token budget used")
    note("briefing", "ok", "openrouter", "nemotron", 9000, detail="fallback after 1")
    note("briefing", "no_answer", detail="3 tried")
    note("briefing", "rejected_future", model="flash-lite", detail="PPI 0.6% vs 0.4%", n=2)
    note("attribution", "not_json", "openrouter", "nemotron", detail="I think...")
    note("attribution", "ok", "gemini", "flash-lite", 2000, at=now - timedelta(days=3))
    s = ai_ledger.summary(7, root=tmp_path, now=now)
    b = next(j for j in s["jobs"] if j["role"] == "briefing")
    assert (b["asked"], b["answered"], b["no_answer"], b["fallbacks"], b["rejected"]) == (3, 2, 1, 1, 2)
    assert b["answer_rate"] == pytest.approx(2 / 3, abs=1e-3) and b["avg_ms"] == 6500 and b["p90_ms"] == 9000
    assert b["failures"] == {"budget_skip": 1} and b["asked_today"] == 3
    a = next(j for j in s["jobs"] if j["role"] == "attribution")
    assert (a["asked"], a["asked_today"], a["failures"]) == (1, 0, {"not_json": 1})
    prov = {p["model"]: p for p in s["providers"]}
    assert prov["gemini/flash-lite"]["ok"] == 2 and prov["groq/gpt-oss-120b"]["budget_skip"] == 1
    assert s["problems"][0]["ts"].startswith("2026-10-18") and len(s["problems"]) == 4


def test_old_days_drop_out_of_the_window(tmp_path):
    now = datetime(2026, 10, 18, tzinfo=UTC)
    ai_ledger.note("briefing", "ok", "gemini", "x", 100, root=tmp_path, now=now - timedelta(days=9))
    assert ai_ledger.summary(7, root=tmp_path, now=now)["jobs"] == []


def test_error_text_never_carries_a_key(tmp_path):
    ai_ledger.note("briefing", "error", "gemini", "x", root=tmp_path,
                   detail="401 for https://x.googleapis.com/v1/models?key=AIzaSyREALKEY123&alt=json Bearer sk-or-abc123")
    d = _rows(tmp_path)[0]["detail"]
    assert "AIzaSyREALKEY123" not in d and "sk-or-abc123" not in d and "REDACTED" in d
    assert ai_ledger.scrub("daily token budget used") == "daily token budget used"     # plain words stay
    assert "s3cr3t" not in ai_ledger.scrub('{"api_key": "s3cr3t"} token=s3cr3t')


@pytest.mark.asyncio
async def test_every_ai_call_outcome_lands_in_the_ledger():
    with patch("collectors.llm_client.chain_for", return_value=[("groq", "fast"), ("gemini", "lite"), ("openrouter", "free")]), \
         patch("collectors.llm_client._call_openai_shaped", new_callable=AsyncMock) as call:
        call.side_effect = [RuntimeError("groq returned 429"), "not json at all", '{"ok": 1}']
        reply = await ask_json("attribution", "sys", "user")
    assert reply.provider == "openrouter"
    rows = _rows(ai_ledger.ROOT)                                   # tests/conftest.py points ROOT at tmp
    assert [r["outcome"] for r in rows] == ["error", "not_json", "ok"]
    assert rows[-1]["detail"] == "fallback after 2" and rows[-1]["provider"] == "openrouter"


@pytest.mark.asyncio
async def test_a_chain_where_nobody_answers_is_one_no_answer():
    with patch("collectors.llm_client.chain_for", return_value=[("groq", "fast")]), \
         patch("collectors.llm_client._call_openai_shaped", new_callable=AsyncMock) as call:
        call.side_effect = [RuntimeError("timeout")]
        await ask_json("news_scoring", "sys", "user")
    assert [r["outcome"] for r in _rows(ai_ledger.ROOT)] == ["error", "no_answer"]


def test_a_future_dated_briefing_event_is_counted_as_thrown_away():
    from collectors.market_briefing import as_news_items
    future = (datetime.now(UTC) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = as_news_items([{"headline": "PPI 0.6% vs 0.4% expected", "event_type": "inflation_data", "score": -0.5,
                            "confidence": 0.8, "when": future, "source": "x"}], "gemini/lite")
    assert items == []
    assert [r["outcome"] for r in _rows(ai_ledger.ROOT)] == ["rejected_future"]


@pytest.mark.asyncio
async def test_the_quality_endpoint_names_jobs_in_plain_english():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler import ai_quality_api
    ai_ledger.note("attribution", "ok", "gemini", "lite", 1200)
    ai_quality_api._cache.update(t=0.0, key=None, body=None)
    app = web.Application()
    ai_quality_api.register(app, None)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/ai/quality?days=7")).json()
    job = body["jobs"][0]
    assert (job["role"], job["name"], job["answered"]) == ("attribution", "Why it moved", 1)
    assert "quota" in body and "problems" in body
