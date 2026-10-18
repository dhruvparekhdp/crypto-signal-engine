"""
GET /api/ai/quality?days=7 - the System > AI tab (owner, 18 Oct 2026, AI plan step 4): per AI job how often it was
asked, answered and by whom, how fast, what failed and why, answers thrown away (future-dated events), and each free
quota left today. Built from data/ai_ledger (collectors/ai_ledger.py) and the budget file. No prompts, answers or keys.
"""
from __future__ import annotations

import time

from aiohttp import web

CACHE_S = 30
_cache: dict = {"t": 0.0, "key": None, "body": None}

# plain-English names for the AI jobs (roles)
JOB_NAMES = {
    "briefing": "Market briefing + big-event monitor", "briefing_calm": "Event monitor (calm checks)",
    "attribution": "Why it moved",
    "news_scoring": "News scoring", "pre_trade": "Pre-trade review", "post_trade": "Post-trade review",
    "position_review": "Open-position review", "research": "Research report", "event_precedent": "Event precedents",
}


def build(days: int) -> dict:
    from collectors import ai_ledger
    from collectors.llm_budget import budget
    from collectors.llm_client import calls_today
    body = ai_ledger.summary(days)
    for j in body["jobs"]:
        j["name"] = JOB_NAMES.get(j["role"], j["role"].replace("_", " ").capitalize())
    try:
        body["quota"] = [{**q, "last_error": ai_ledger.scrub(q.get("last_error") or "")} for q in budget().snapshot()]
    except Exception:  # noqa: BLE001 - the tab still shows the ledger
        body["quota"] = []
    try:
        body["asked_today_by_role"] = calls_today()
    except Exception:  # noqa: BLE001
        body["asked_today_by_role"] = {}
    return body


def register(app: web.Application, runner) -> None:
    async def quality(request: web.Request) -> web.Response:
        try:
            days = max(1, min(30, int(request.query.get("days", "7"))))
        except ValueError:
            days = 7
        if _cache["body"] is None or _cache["key"] != days or time.time() - _cache["t"] > CACHE_S:
            _cache.update(t=time.time(), key=days, body=build(days))
        return web.json_response(_cache["body"])

    app.router.add_get("/api/ai/quality", quality)
