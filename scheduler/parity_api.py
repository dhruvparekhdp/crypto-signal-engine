"""
Backtest = live check, for the portal (owner, 19 Oct 2026). analysis/parity.py has the rules; the runner runs it daily.

    GET /api/parity          admin only - the last report (live positions are in it)
    GET /api/parity?run=1    admin only - start a fresh run now (takes about a minute); the page polls for it
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp import web


def register(app: web.Application, runner) -> None:
    state: dict = {"task": None}

    async def parity(request: web.Request) -> web.Response:
        from scheduler.settings_page import _admin
        if await _admin(request) is not None:
            return web.json_response({"error": "admin login needed: sign in on Settings"}, status=401)
        running = state["task"] is not None and not state["task"].done()
        if request.query.get("run") and not running and runner is not None:
            state["task"] = asyncio.create_task(runner._parity_job(alert=False))
            running = True
        report = getattr(runner, "_parity_last", None) if runner is not None else None
        if report is None:
            try:
                report = json.loads(Path(getattr(runner, "_PARITY_FILE", "data/parity_report.json")).read_text())
            except (OSError, ValueError):
                report = None
        return web.json_response({"running": running, "report": report}, dumps=lambda o: json.dumps(o, default=str))

    app.router.add_get("/api/parity", parity)
