"""SSE /api/stream — read-only heartbeat + optional price ticks (plan §6.4)."""
from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web


async def api_stream(_runner, request: web.Request) -> web.StreamResponse:
    """Default-off friendly: always safe read-only events; clients fall back to polling."""
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    try:
        for _ in range(120):
            if request.transport is not None and request.transport.is_closing():
                break
            payload = {"ts": time.time()}
            await resp.write(f"event: heartbeat\ndata: {json.dumps(payload)}\n\n".encode())
            await asyncio.sleep(15)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


def register_api_stream(app: web.Application, runner) -> None:
    async def _bound(req: web.Request) -> web.StreamResponse:
        return await api_stream(runner, req)

    app.router.add_get("/api/stream", _bound)
