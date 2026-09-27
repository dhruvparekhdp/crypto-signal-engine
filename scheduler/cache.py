"""
A small process-local cache for read-heavy endpoints whose answer barely
changes between one poll and the next.

Why not Redis, or anything else running 24/7 as its own service: this app
is one Python process on one server. A plain dict already lives at the
same latency Redis would if it ran locally too — near zero, no network hop
at all — without a second always-on process to install, patch, back up,
and leave open on a box that is already being probed by scanners (see
/api-docs's finding of .env/.git scan attempts). Redis earns its keep once
more than one process needs to SHARE a cache; this app has exactly one
process, so the shared part of "shared, always-on cache" buys nothing here.

Every entry has a time-to-live: a cache with no expiry is a second source
of truth waiting to disagree with the database. A setting saved on
/settings that a stale cache kept answering with the old value would be
worse than the slow page it replaced.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

_store: dict[str, tuple[float, object]] = {}


async def cached(key: str, ttl: float, fetch: Callable[[], Awaitable[T]]) -> T:
    """
    The cached value for `key` if it was fetched less than `ttl` seconds
    ago; otherwise call `fetch()`, cache what it returns, and return that.

    Concurrent callers within the same tiny window can still both miss and
    both fetch — the target here is collapsing a burst of polls a few
    seconds apart, not perfect single-flight de-duplication, which would
    cost more code than the rare double-fetch it would save.
    """
    hit = _store.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = await fetch()
    _store[key] = (now, value)
    return value


def invalidate(*keys: str) -> None:
    """Drop cached entries immediately — call this right after a write
    that the next read must see, rather than waiting out the TTL."""
    for k in keys:
        _store.pop(k, None)


def clear() -> None:
    _store.clear()


def stats() -> dict:
    """What's cached and how many seconds old, for /api/debug/perf — so
    caching is something you can see, not a silent thing to wonder about."""
    now = time.monotonic()
    return {k: round(now - at, 1) for k, (at, _) in _store.items()}
