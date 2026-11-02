"""Angular /app/ static hosting (plan §6.1)."""
from __future__ import annotations

import unittest
from pathlib import Path

from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import web

from scheduler.web_static import register_web_app


class TestWebAppStatic(AioHTTPTestCase):
    async def get_application(self):
        app = web.Application()
        register_web_app(app)
        return app

    async def test_index_fallback_and_hashed_cache(self):
        root = Path(__file__).resolve().parents[1] / "web" / "dist" / "browser"
        root.mkdir(parents=True, exist_ok=True)
        (root / "index.html").write_text("<html>app</html>", encoding="utf-8")
        hashed = root / "main.a1b2c3d4e5f67890.js"
        hashed.write_text("console.log(1)", encoding="utf-8")
        try:
            r = await self.client.get("/app/command/overview")
            self.assertEqual(r.status, 200)
            self.assertIn(b"app", await r.read())
            r2 = await self.client.get("/app/main.a1b2c3d4e5f67890.js")
            self.assertEqual(r2.status, 200)
            self.assertIn("immutable", r2.headers.get("Cache-Control", ""))
        finally:
            hashed.unlink(missing_ok=True)
            (root / "index.html").unlink(missing_ok=True)
