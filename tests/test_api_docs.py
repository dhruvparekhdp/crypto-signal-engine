"""The /api-docs page lists exactly the routes the server has."""
from __future__ import annotations

from aiohttp.test_utils import AioHTTPTestCase

from scheduler.api_docs import DIAGNOSTICS, ENDPOINTS
from tests.test_api_security import FakeRunner


class TestApiDocs(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app
        return await make_app(FakeRunner())

    def _routes(self):
        return {(r.method, r.resource.canonical) for r in self.app.router.routes()
                if r.resource is not None and r.method in ("GET", "POST")}

    async def test_every_listed_endpoint_exists(self):
        routes = self._routes()
        for _, method, path, _, _ in ENDPOINTS:
            with self.subTest(path=path):
                self.assertIn((method, path), routes)

    async def test_every_api_is_listed(self):
        listed = {(m, p) for _, m, p, _, _ in ENDPOINTS}
        missing = sorted(p for m, p in self._routes()
                         if p.startswith("/api/") and (m, p) not in listed
                         and p not in ("/api/auth/verify", "/api/settings/auth/login",
                                       "/api/settings/auth/logout", "/api/settings/toggle",
                                       "/api/strategy/config"))
        self.assertEqual(missing, [])

    async def test_diagnostics_are_listed_gets(self):
        gets = {p for _, m, p, _, _ in ENDPOINTS if m == "GET"}
        self.assertTrue(set(DIAGNOSTICS) <= gets)

    async def test_the_page_renders_with_the_spec_and_the_shared_clock(self):
        resp = await self.client.get("/api-docs")
        self.assertEqual(resp.status, 200)
        html = await resp.text()
        self.assertNotIn("__SPEC__", html)
        self.assertIn('"/api/pipeline"', html)
        self.assertIn("window.fmtStamp = function", html)
