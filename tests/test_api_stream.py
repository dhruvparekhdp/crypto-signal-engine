"""SSE /api/stream read-only."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from aiohttp.test_utils import AioHTTPTestCase


class TestApiStream(AioHTTPTestCase):
    async def get_application(self):
        from aiohttp import web
        from scheduler.api_stream import register_api_stream

        app = web.Application()
        register_api_stream(app, MagicMock())
        return app

    async def test_stream_returns_event_stream(self):
        resp = await self.client.get("/api/stream")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
        chunk = await asyncio.wait_for(resp.content.read(512), timeout=3)
        self.assertIn(b"heartbeat", chunk)
        resp.close()
