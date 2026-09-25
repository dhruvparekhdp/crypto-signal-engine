"""
Auth, rate limiting and headers for the HTTP API.

Three endpoints — settings toggle, watchlist add, watchlist remove — had no
protection at all before this: anyone with the URL could change what the
server does. These tests pin the fix at two levels: the pure functions in
`scheduler.security` in isolation, and the whole app wired together through
aiohttp's own test client, so a route that forgets to call the guard fails
here rather than in production.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from scheduler.security import RateLimiter, check_bearer_auth, client_ip

# ── Pure unit tests: no network, no event loop ──────────────────────────────


class FakeRequest:
    """Just enough of aiohttp.web.Request for the functions under test."""

    def __init__(self, headers=None, remote="203.0.113.9"):
        self.headers = headers or {}
        self.remote = remote


class TestCheckBearerAuth(unittest.TestCase):
    def test_an_unconfigured_token_refuses_everything(self):
        """Fails closed: empty token means the endpoint is off, not open."""
        req = FakeRequest({"Authorization": "Bearer anything"})
        resp = check_bearer_auth(req, token="")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status, 503)

    def test_no_header_is_refused(self):
        resp = check_bearer_auth(FakeRequest({}), token="secret123")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status, 401)

    def test_wrong_token_is_refused(self):
        req = FakeRequest({"Authorization": "Bearer nope"})
        resp = check_bearer_auth(req, token="secret123")
        self.assertEqual(resp.status, 401)

    def test_correct_token_is_accepted(self):
        req = FakeRequest({"Authorization": "Bearer secret123"})
        self.assertIsNone(check_bearer_auth(req, token="secret123"))

    def test_a_token_without_the_bearer_prefix_is_refused(self):
        """The scheme matters — a bare token in the header is not a bearer token."""
        req = FakeRequest({"Authorization": "secret123"})
        resp = check_bearer_auth(req, token="secret123")
        self.assertEqual(resp.status, 401)

    def test_the_query_string_is_never_consulted(self):
        """
        No fallback to ?token=. A token in a URL ends up in proxy logs,
        browser history and Referer headers — the header-only design is
        deliberate, not an oversight to fill in later.
        """
        req = FakeRequest({})
        req.query = {"token": "secret123"}  # noqa: B950 - simulating a URL param
        resp = check_bearer_auth(req, token="secret123")
        self.assertEqual(resp.status, 401)


class TestClientIP(unittest.TestCase):
    def test_uses_the_first_forwarded_address_behind_a_trusted_proxy(self):
        """
        Each proxy hop appends its own address, so the client's own address
        is the FIRST entry, not the last.
        """
        req = FakeRequest({"X-Forwarded-For": "198.51.100.1, 10.0.0.5, 10.0.0.6"},
                          remote="10.0.0.6")
        self.assertEqual(client_ip(req, frozenset({"10.0.0.6"})), "198.51.100.1")

    def test_a_client_cannot_choose_its_own_rate_limit_key(self):
        """
        With no proxy in front, the header is whatever the attacker typed:
        rotating it must not buy a fresh login budget.
        """
        req = FakeRequest({"X-Forwarded-For": "203.0.113.99"}, remote="192.0.2.4")
        self.assertEqual(client_ip(req, frozenset()), "192.0.2.4")

    def test_falls_back_to_remote_without_the_header(self):
        req = FakeRequest({}, remote="192.0.2.4")
        self.assertEqual(client_ip(req), "192.0.2.4")

    def test_an_empty_header_also_falls_back(self):
        req = FakeRequest({"X-Forwarded-For": ""}, remote="192.0.2.4")
        self.assertEqual(client_ip(req), "192.0.2.4")


class TestRateLimiter(unittest.TestCase):
    def test_requests_under_the_limit_are_allowed(self):
        limiter = RateLimiter()
        for _ in range(5):
            allowed, _ = limiter.allow("k", limit=5, window_seconds=60)
            self.assertTrue(allowed)

    def test_the_request_that_exceeds_the_limit_is_refused(self):
        limiter = RateLimiter()
        for _ in range(5):
            limiter.allow("k", limit=5, window_seconds=60)
        allowed, retry_after = limiter.allow("k", limit=5, window_seconds=60)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_different_keys_have_independent_budgets(self):
        """One IP hitting its limit must not affect another IP's bucket."""
        limiter = RateLimiter()
        for _ in range(5):
            limiter.allow("ip-a", limit=5, window_seconds=60)
        allowed, _ = limiter.allow("ip-b", limit=5, window_seconds=60)
        self.assertTrue(allowed)

    def test_the_window_slides_rather_than_resetting_in_lockstep(self):
        """
        A request from long ago must not count against a fresh window — the
        deque drops anything older than `window_seconds` before counting.
        """
        limiter = RateLimiter()
        allowed, _ = limiter.allow("k", limit=1, window_seconds=0.001)
        self.assertTrue(allowed)
        import time
        time.sleep(0.01)
        allowed, _ = limiter.allow("k", limit=1, window_seconds=0.001)
        self.assertTrue(allowed)

    def test_forget_stale_keys_drops_only_the_stale_ones(self):
        limiter = RateLimiter()
        limiter.allow("stale", limit=10, window_seconds=60)
        limiter._hits["stale"][-1] -= 10_000  # force it into the past
        limiter.allow("fresh", limit=10, window_seconds=60)
        dropped = limiter.forget_stale_keys(older_than_seconds=1.0)
        self.assertEqual(dropped, 1)
        self.assertIn("fresh", limiter._hits)
        self.assertNotIn("stale", limiter._hits)


# ── Integration: the real app, wired together ───────────────────────────────


class FakeCryptoStore:
    """Enough of CryptoStateStore for /api/crypto/coins to render an empty board."""

    async def get_all(self):
        return []

    async def get_symbols(self):
        return ["btcusdt"]


class FakeRunner:
    """Just enough of the real Runner for the three protected handlers."""

    def __init__(self):
        self.collector_enabled = {"sportradar": True}
        self.added = []
        self.removed = []
        self.crypto_store = FakeCryptoStore()

    async def add_crypto_symbol(self, symbol: str) -> None:
        self.added.append(symbol)

    async def remove_crypto_symbol(self, symbol: str) -> None:
        self.removed.append(symbol)


class TestProtectedEndpoints(AioHTTPTestCase):
    """
    Through the real aiohttp app, not a reimplementation of the routing —
    this is the level at which "did someone forget to call the guard" and
    "is the middleware actually registered" would fail.
    """

    async def get_application(self):
        from config.settings import settings
        from scheduler.health import make_app

        settings.api_auth_token = "test-token-abc"
        settings.api_rate_limit_requests = 1000
        settings.api_rate_limit_window_seconds = 60
        settings.api_auth_rate_limit_requests = 1000
        settings.api_auth_rate_limit_window_seconds = 60
        self.runner = FakeRunner()
        return await make_app(self.runner)

    async def setUpAsync(self):
        await super().setUpAsync()
        self._verify_patch = patch(
            "scheduler.health._verify_admin_session",
            new=AsyncMock(return_value=False),
        )
        self._verify_patch.start()

    async def tearDownAsync(self):
        self._verify_patch.stop()
        await super().tearDownAsync()

    async def test_toggle_without_a_token_is_refused(self):
        resp = await self.client.post("/api/settings/toggle",
                                      json={"collector": "sportradar", "enabled": False})
        self.assertEqual(resp.status, 401)
        self.assertTrue(self.runner.collector_enabled["sportradar"])

    async def test_toggle_with_the_right_token_works(self):
        resp = await self.client.post(
            "/api/settings/toggle",
            json={"collector": "sportradar", "enabled": False},
            headers={"Authorization": "Bearer test-token-abc"})
        self.assertEqual(resp.status, 200)
        self.assertFalse(self.runner.collector_enabled["sportradar"])

    async def test_watchlist_add_without_a_token_is_refused(self):
        resp = await self.client.post("/api/crypto/watchlist/add", json={"symbol": "dogeusdt"})
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.runner.added, [])

    async def test_watchlist_add_with_the_right_token_works(self):
        with patch("collectors.binance_symbols.is_listed", new=AsyncMock(return_value=True)):
            resp = await self.client.post(
                "/api/crypto/watchlist/add", json={"symbol": "dogeusdt"},
                headers={"Authorization": "Bearer test-token-abc"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.runner.added, ["dogeusdt"])

    async def test_a_pair_binance_does_not_list_is_refused(self):
        """xauusdt is the case that mattered: no candles, then a stand-in, then 4.3e-05."""
        with patch("collectors.binance_symbols.is_listed", new=AsyncMock(return_value=False)):
            resp = await self.client.post(
                "/api/crypto/watchlist/add", json={"symbol": "xauusdt"},
                headers={"Authorization": "Bearer test-token-abc"})
        self.assertEqual(resp.status, 400)
        self.assertIn("not a Binance", (await resp.json())["error"])
        self.assertEqual(self.runner.added, [])

    async def test_an_unreachable_binance_does_not_block_adding(self):
        """Unknown is not unlisted: add it, and say it was not confirmed."""
        with patch("collectors.binance_symbols.is_listed", new=AsyncMock(return_value=None)):
            resp = await self.client.post(
                "/api/crypto/watchlist/add", json={"symbol": "paxgusdt"},
                headers={"Authorization": "Bearer test-token-abc"})
        self.assertEqual(resp.status, 200)
        self.assertIn("warning", await resp.json())
        self.assertEqual(self.runner.added, ["paxgusdt"])

    async def test_the_watchlist_is_readable_without_a_token(self):
        resp = await self.client.get("/api/crypto/watchlist")
        self.assertEqual(resp.status, 200)
        self.assertIn("symbols", await resp.json())

    async def test_watchlist_remove_without_a_token_is_refused(self):
        resp = await self.client.post("/api/crypto/watchlist/remove", json={"symbol": "dogeusdt"})
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.runner.removed, [])

    async def test_auth_verify_reports_a_good_token(self):
        resp = await self.client.post(
            "/api/auth/verify", headers={"Authorization": "Bearer test-token-abc"})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"])

    async def test_auth_verify_reports_a_bad_token(self):
        resp = await self.client.post(
            "/api/auth/verify", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status, 401)

    async def test_read_endpoints_stay_open(self):
        """
        Market data carries no secret and the existing dashboard reads it
        without a token — gating it would break the deployed product for no
        security benefit. Only the three mutating endpoints require auth.
        """
        resp = await self.client.get("/api/crypto/coins")
        self.assertNotEqual(resp.status, 401)

    async def test_security_headers_are_present_on_every_response(self):
        resp = await self.client.get("/api/crypto/coins")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")

    async def test_api_responses_are_never_cached(self):
        resp = await self.client.get("/api/crypto/coins")
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    async def test_html_pages_are_not_marked_no_store(self):
        """The no-store rule is for API JSON, not for the page shell itself."""
        resp = await self.client.get("/")
        self.assertNotEqual(resp.headers.get("Cache-Control"), "no-store")


class TestRateLimitIntegration(AioHTTPTestCase):
    async def get_application(self):
        from config.settings import settings
        from scheduler.health import make_app

        settings.api_auth_token = ""
        settings.api_rate_limit_requests = 3
        settings.api_rate_limit_window_seconds = 60
        settings.api_auth_rate_limit_requests = 2
        settings.api_auth_rate_limit_window_seconds = 60
        return await make_app(FakeRunner())

    async def tearDownAsync(self):
        from config.settings import settings
        settings.api_rate_limit_requests = 120
        settings.api_rate_limit_window_seconds = 60
        settings.api_auth_rate_limit_requests = 5
        settings.api_auth_rate_limit_window_seconds = 60
        await super().tearDownAsync()

    async def test_a_burst_over_the_general_limit_gets_429(self):
        for _ in range(3):
            resp = await self.client.get("/api/crypto/coins")
            self.assertEqual(resp.status, 200)
        resp = await self.client.get("/api/crypto/coins")
        self.assertEqual(resp.status, 429)
        self.assertIn("Retry-After", resp.headers)

    async def test_the_auth_endpoint_has_its_own_tighter_bucket(self):
        """
        Configured to 2 here vs 3 for the general bucket, and hit only via
        /api/auth/verify — confirms the two limiters are genuinely separate,
        not one shared counter keyed only by IP. No token is configured in
        this test class, so the first two calls reach the handler and get
        503 (unconfigured) rather than 401 — the rate limiter runs before
        the handler and does not care what the handler would have said.
        """
        for _ in range(2):
            resp = await self.client.post("/api/auth/verify")
            self.assertEqual(resp.status, 503)
        resp = await self.client.post("/api/auth/verify")
        self.assertEqual(resp.status, 429)

    async def test_hitting_the_auth_limit_does_not_touch_the_general_bucket(self):
        for _ in range(2):
            await self.client.post("/api/auth/verify")
        await self.client.post("/api/auth/verify")  # exhausts the auth bucket

        # General bucket (limit 3) should still have room.
        for _ in range(3):
            resp = await self.client.get("/api/crypto/coins")
            self.assertEqual(resp.status, 200)

    async def test_the_dashboard_login_shares_the_tight_bucket(self):
        """
        The password login belongs with the token check, not with the
        dashboard's polling traffic.

        It sat in the general bucket for a while, which meant the endpoint
        guarding the admin password allowed 120 attempts a minute while the
        endpoint guarding the API token allowed 10 per five minutes — exactly
        backwards, since the password is the shorter secret of the two.
        """
        for _ in range(2):
            await self.client.post("/api/settings/auth/login", json={"password": "x"})
        resp = await self.client.post("/api/settings/auth/login", json={"password": "x"})
        self.assertEqual(resp.status, 429)

    async def test_the_token_check_and_the_login_share_one_counter(self):
        """
        Two tight endpoints with two separate budgets would double the
        guesses available per window, which defeats tightening either.
        """
        await self.client.post("/api/auth/verify")
        await self.client.post("/api/settings/auth/login", json={"password": "x"})
        resp = await self.client.post("/api/auth/verify")
        self.assertEqual(resp.status, 429)


if __name__ == "__main__":
    unittest.main()
