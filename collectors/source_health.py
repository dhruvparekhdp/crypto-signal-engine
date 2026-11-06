"""Last success, error count and latency for each data feed.

A feed is fresh, degraded, stale, or erroring from thresholds on age, consecutive
errors, and latency. The numbers are logged and served at
``GET /integration/v1/health``. A notify hook can tell the existing notifier;
it stays off unless ``notify_enabled`` is set, so a bad feed never pages
Telegram on its own.

Logs stay at info/warning. ``scheduler.alerts`` turns error-level logs into
Telegram messages, and this tracker must not take that path while the hook
is off.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger()

FRESH = "fresh"
DEGRADED = "degraded"
STALE = "stale"
ERRORING = "erroring"

Notify = Callable[[str], Any]
Failed = Callable[[Any], bool]


@dataclass(frozen=True)
class Thresholds:
    """When a feed leaves fresh.

    ``degraded_after_errors`` and ``erroring_after_errors`` count consecutive
    failures since the last success. ``error_count`` on the snapshot is the
    lifetime total and is not reset by a later success.
    """

    stale_after_s: float = 300.0
    degraded_after_errors: int = 1
    erroring_after_errors: int = 3
    degraded_latency_ms: float = 5_000.0


@dataclass
class _Feed:
    name: str
    first_seen_at: datetime
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    error_count: int = 0
    consecutive_errors: int = 0
    last_latency_ms: float | None = None
    last_error: str = ""
    state: str | None = None


def judge(feed: _Feed, now: datetime, limits: Thresholds) -> str:
    """fresh, degraded, stale, or erroring. An error streak wins over silence."""
    if feed.consecutive_errors >= limits.erroring_after_errors:
        return ERRORING
    anchor = feed.last_success_at or feed.first_seen_at
    age_s = (now - anchor).total_seconds()
    if age_s >= limits.stale_after_s:
        return STALE
    slow = (
        feed.last_latency_ms is not None
        and feed.last_latency_ms > limits.degraded_latency_ms
    )
    if feed.consecutive_errors >= limits.degraded_after_errors or slow:
        return DEGRADED
    return FRESH


class SourceHealth:
    """In-memory feed book. Pass ``now`` to keep a replay deterministic."""

    def __init__(
        self,
        thresholds: Thresholds | None = None,
        *,
        notify: Notify | None = None,
        notify_enabled: bool = False,
    ) -> None:
        self.thresholds = thresholds or Thresholds()
        self.notify = notify
        self.notify_enabled = notify_enabled
        self._feeds: dict[str, _Feed] = {}

    def bind(
        self,
        *,
        thresholds: Thresholds | None = None,
        notify: Notify | None = None,
        notify_enabled: bool = False,
    ) -> None:
        if thresholds is not None:
            self.thresholds = thresholds
        self.notify = notify
        self.notify_enabled = notify_enabled

    def record_success(
        self,
        name: str,
        latency_ms: float,
        *,
        now: datetime | None = None,
    ) -> str:
        when = _clock(now)
        feed = self._feed(name, when)
        feed.last_success_at = when
        feed.consecutive_errors = 0
        feed.last_error = ""
        feed.last_latency_ms = round(float(latency_ms), 3)
        return self._finish(feed, when)

    def record_error(
        self,
        name: str,
        *,
        latency_ms: float | None = None,
        error: str = "",
        now: datetime | None = None,
    ) -> str:
        when = _clock(now)
        feed = self._feed(name, when)
        feed.error_count += 1
        feed.consecutive_errors += 1
        feed.last_error_at = when
        feed.last_error = str(error)[:200]
        if latency_ms is not None:
            feed.last_latency_ms = round(float(latency_ms), 3)
        return self._finish(feed, when)

    def state_of(self, name: str, *, now: datetime | None = None) -> str | None:
        feed = self._feeds.get(name)
        if feed is None:
            return None
        return judge(feed, _clock(now), self.thresholds)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        when = _clock(now)
        feeds = {
            name: _public(feed, judge(feed, when, self.thresholds))
            for name, feed in sorted(self._feeds.items())
        }
        return {"feeds": feeds}

    def log_snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        payload = self.snapshot(now=now)
        log.info("source_health", feeds=payload["feeds"])
        return payload

    def _feed(self, name: str, when: datetime) -> _Feed:
        feed = self._feeds.get(name)
        if feed is None:
            feed = _Feed(name=name, first_seen_at=when)
            self._feeds[name] = feed
        return feed

    def _finish(self, feed: _Feed, when: datetime) -> str:
        previous = feed.state
        state = judge(feed, when, self.thresholds)
        feed.state = state
        self._log(feed, state)
        self._notify(feed, previous, state)
        return state

    def _log(self, feed: _Feed, state: str) -> None:
        fields = {
            "feed": feed.name,
            "state": state,
            "last_success_at": _iso(feed.last_success_at),
            "error_count": feed.error_count,
            "latency_ms": feed.last_latency_ms,
        }
        if state == FRESH:
            log.info("source_feed", **fields)
        else:
            log.warning("source_feed", **fields)

    def _notify(self, feed: _Feed, previous: str | None, state: str) -> None:
        if not self.notify_enabled or self.notify is None or previous == state:
            return
        # A first successful poll is not news. A change into a bad state, or
        # back to fresh, is.
        if previous is None and state == FRESH:
            return
        text = (
            f"Feed {feed.name} is {state}. "
            f"errors={feed.error_count} latency_ms={feed.last_latency_ms} "
            f"last_success={_iso(feed.last_success_at)}"
        )
        try:
            self.notify(text)
        except Exception:
            log.warning("source_health_notify_failed", feed=feed.name)


def _clock(now: datetime | None) -> datetime:
    when = now or datetime.now(UTC)
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when


def _iso(when: datetime | None) -> str | None:
    return None if when is None else when.isoformat()


def _public(feed: _Feed, state: str) -> dict[str, Any]:
    return {
        "state": state,
        "last_success_at": _iso(feed.last_success_at),
        "last_error_at": _iso(feed.last_error_at),
        "error_count": feed.error_count,
        "consecutive_errors": feed.consecutive_errors,
        "last_latency_ms": feed.last_latency_ms,
        "last_error": feed.last_error,
    }


_health: SourceHealth | None = None


def get_health() -> SourceHealth:
    global _health
    if _health is None:
        _health = SourceHealth()
    return _health


def reset_health(book: SourceHealth | None = None) -> SourceHealth:
    """Test helper. Production uses :func:`get_health`."""
    global _health
    _health = book or SourceHealth()
    return _health


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


async def watch(
    name: str,
    call: Awaitable[Any],
    *,
    failed: Failed,
    health: SourceHealth | None = None,
) -> Any:
    """Time one poll. ``failed(result)`` means the call returned without an update."""
    book = health or get_health()
    started = time.perf_counter()
    try:
        result = await call
    except Exception:
        book.record_error(name, latency_ms=_elapsed_ms(started))
        raise
    latency = _elapsed_ms(started)
    if failed(result):
        book.record_error(name, latency_ms=latency)
    else:
        book.record_success(name, latency_ms=latency)
    return result
