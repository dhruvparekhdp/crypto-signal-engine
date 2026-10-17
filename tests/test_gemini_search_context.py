"""18 Oct 2026: Gemini runs the briefing/attribution jobs with OUR headlines (+ web snippets) in the prompt - never
Google's grounding tool (429, no free quota) - so the jobs stop burning Groq's free daily tokens."""
import asyncio

import pytest

from collectors import llm_client as lc


@pytest.mark.asyncio
async def test_gemini_search_injects_our_headlines_and_sends_no_grounding_tool(monkeypatch):
    sent = {}

    async def heads():
        return "- 10-18 03:00 [coindesk] Bitcoin ETFs see $200M inflows"
    monkeypatch.setattr(lc, "_fresh_headlines_block", heads)
    monkeypatch.setattr(lc, "_search_ddg", lambda q, n: "")

    class Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **k):
            sent.update(url=url, payload=json)
            return Resp()
    monkeypatch.setattr(lc.httpx, "AsyncClient", Client)
    monkeypatch.setattr(lc.settings, "gemini_api_key", "k", raising=False)
    await lc._call_openai_shaped(lc.PROVIDERS["gemini"], "gemini-3.5-flash-lite+search", "SYS", "Brief me.", 300, 0.2, 20)
    p = sent["payload"]
    assert p["model"] == "gemini-3.5-flash-lite" and "tools" not in p
    assert "Bitcoin ETFs see $200M inflows" in p["messages"][0]["content"]
    assert "never report a result for something dated after the current time" in p["messages"][0]["content"]


def test_gemini_free_limits_are_known_to_the_budget():
    from collectors.llm_budget import LIMITS
    assert LIMITS["gemini/gemini-3.5-flash-lite"]["rpd"] == 500 and LIMITS["gemini/*"]["rpd"] == 20
