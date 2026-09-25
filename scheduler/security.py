"""
Auth, rate limiting and response headers for the HTTP API.

Why this exists
----------------
Three endpoints had no protection at all: anyone with the URL could toggle a
data collector or edit the watchlist. That was tolerable while the only
client was a browser sitting on the same trust as the operator; it stops
being tolerable the moment a second client — the iOS app — talks to this API
over the open internet.

Scope, deliberately narrow
---------------------------
This is bearer-token auth for a SINGLE operator, not a multi-user auth
system. One shared secret, set once as an environment variable, never
generated or rotated by the server itself. That is the right amount of
machinery for one person's phone talking to one person's server — building
sessions, OAuth or per-user accounts here would be solving a problem this
app does not have.

Fails closed. `check_bearer_auth` refuses every request when the token is
unconfigured (empty), the same convention `sentiment_ingest_token` already
uses. A protected endpoint that is "open until you remember to configure it"
is worse than one that visibly refuses to work — the second gets fixed
immediately, the first gets forgotten.

Rate limiting is a separate concern from auth and applies whether or not a
request is authenticated: a leaked or guessed token should not buy unlimited
requests, and the endpoints that exist to accept a secret — the token check
and the dashboard login — get a much tighter bucket than everything else,
because those are the ones worth grinding against.
"""
from __future__ import annotations

import hmac
import time
from collections import deque

from aiohttp import web

# Client IP, behind a proxy
# -------------------------
# Render (like every PaaS) terminates TLS at a proxy in front of this
# process, so `request.remote` is the proxy's address — every request would
# land in the same rate-limit bucket, and one client could exhaust it for
# everyone. X-Forwarded-For carries the real chain; the FIRST entry is the
# original client by convention (each hop appends its own address to the
# end), so that is the one this trusts.


def client_ip(request: web.Request, trusted_proxies: frozenset[str] | None = None) -> str:
    """
    The address rate limits are keyed on.

    X-Forwarded-For is written by whoever sends the request, so it is only
    believed when the connection itself comes from a proxy we run. The engine
    listens on :8080 with no proxy in front, so by default the header is
    ignored — otherwise every guess at the login can carry a fresh made-up
    address and never meet the limit.
    """
    remote = request.remote or "unknown"
    if trusted_proxies is None:
        from config.settings import settings
        trusted_proxies = frozenset(
            p.strip() for p in settings.trusted_proxy_ips.split(",") if p.strip())
    if remote in trusted_proxies:
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return remote


# ── Bearer auth ─────────────────────────────────────────────────────────────


def check_bearer_auth(request: web.Request, token: str) -> web.Response | None:
    """
    Returns an error Response if the request should be refused, else None.

    Header only, not a query-string fallback — a token in the URL ends up in
    proxy logs, browser history and Referer headers, which defeats the point
    of a secret. `hmac.compare_digest` because a plain `==` returns as soon
    as the first character differs, leaking the token one character at a
    time to anyone able to measure response latency closely enough.
    """
    if not token:
        return web.json_response(
            {"error": "not configured", "hint": "set API_AUTH_TOKEN"}, status=503)

    header = request.headers.get("Authorization", "")
    supplied = header[7:] if header.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, token):
        return web.json_response({"error": "unauthorized"}, status=401)
    return None


# ── Rate limiting ────────────────────────────────────────────────────────────


class RateLimiter:
    """
    Sliding-window request counter, per key.

    In-memory and unlocked: aiohttp's default runtime is single-threaded
    asyncio, so two requests never touch a deque at the same instant, and a
    lock here would only protect against a race that cannot occur. It does
    NOT survive a restart or scale across multiple instances — for one
    operator's server that trade-off is the right one; a shared store would
    be solving a scaling problem this deployment does not have.

    Each key keeps a deque of the timestamps of its recent requests. `allow`
    drops everything older than the window, and admits the new request only
    if what is left is under the limit. Memory is bounded by the number of
    distinct keys seen recently, not by request volume, since old timestamps
    are always trimmed before counting.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        """
        (allowed, retry_after_seconds). retry_after is 0.0 when allowed —
        callers should not set a Retry-After header on a successful request.
        """
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = bucket[0] + window_seconds - now
            return False, max(0.0, retry_after)

        bucket.append(now)
        return True, 0.0

    def forget_stale_keys(self, older_than_seconds: float = 3600.0) -> int:
        """
        Drop keys with no recent hits, so a long-running process does not
        accumulate one deque per IP that has ever visited. Not called
        automatically — a caller with a scheduler (the app already has one)
        should run this periodically; a request-time middleware is the wrong
        place to do unrelated cleanup work on someone else's request.
        """
        now = time.monotonic()
        stale = [k for k, bucket in self._hits.items()
                 if not bucket or bucket[-1] < now - older_than_seconds]
        for k in stale:
            del self._hits[k]
        return len(stale)


# Endpoints whose whole job is accepting an attempt at a secret. Every one of
# these belongs in the tight bucket; /api/auth/verify was the only member for
# a while, which left the dashboard's own login — the endpoint that actually
# guards the admin password — sharing the roomy bucket the dashboard uses for
# polling, so several hundred guesses an hour passed unremarked.
_SECRET_GUESSING_PATHS = frozenset({
    "/api/auth/verify",
    "/api/settings/auth/login",
})


def rate_limit_middleware(get_settings):
    """
    Factory so the limits are read from settings at request time, not
    baked in at app-startup — a config change takes effect without a
    redeploy of the middleware itself, only of the settings that feed it.

    The two RateLimiter instances live inside this closure rather than as
    module-level singletons, so every call to `make_app()` — including one
    per test case — starts with a clean budget instead of sharing counters
    with whatever else happened to run earlier in the same process. Two
    buckets, not one: the general limiter covers ordinary API traffic (the
    dashboard polling every 20s, the iOS app checking in), and the auth
    limiter covers ONLY the endpoints whose job is accepting attempts at a
    secret — it must be far tighter, and mixing the two would let a
    burst of legitimate reads use up the headroom meant to slow down someone
    guessing the token.

    Scoped to `/api/` — the HTML pages are static markup with no per-request
    cost worth bounding, and rate-limiting them would only make the
    dashboard itself flaky on a slow connection for no security benefit.
    """
    general_limiter = RateLimiter()
    auth_limiter = RateLimiter()

    @web.middleware
    async def _middleware(request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/"):
            return await handler(request)

        settings = get_settings()
        ip = client_ip(request)

        if path in _SECRET_GUESSING_PATHS:
            limiter, limit, window, bucket = (
                auth_limiter, settings.api_auth_rate_limit_requests,
                settings.api_auth_rate_limit_window_seconds, "auth")
        else:
            limiter, limit, window, bucket = (
                general_limiter, settings.api_rate_limit_requests,
                settings.api_rate_limit_window_seconds, "api")

        allowed, retry_after = limiter.allow(f"{bucket}:{ip}", limit, window)
        if not allowed:
            resp = web.json_response(
                {"error": "rate limited", "retry_after_seconds": round(retry_after, 1)},
                status=429)
            resp.headers["Retry-After"] = str(max(1, round(retry_after)))
            return resp

        return await handler(request)

    return _middleware


# ── Response headers ─────────────────────────────────────────────────────────


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """
    A handful of headers with no behavioural cost and a real, if modest,
    defensive payoff. Applied to every response, HTML and JSON alike.
    """
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        # Market data and account state change by the second; a cache
        # serving a stale response here is a correctness bug, not just a
        # staleness inconvenience, so this is stronger than a max-age.
        response.headers.setdefault("Cache-Control", "no-store")
    return response
