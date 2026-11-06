"""Feed health: a fresh poll, a silent feed, and a feed that keeps failing."""
from datetime import UTC, datetime, timedelta

import pytest

from collectors.source_health import (
    DEGRADED,
    ERRORING,
    FRESH,
    STALE,
    SourceHealth,
    Thresholds,
    reset_health,
    watch,
)

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
LIMITS = Thresholds(
    stale_after_s=300.0,
    degraded_after_errors=1,
    erroring_after_errors=3,
    degraded_latency_ms=5_000.0,
)


def book(**kwargs) -> SourceHealth:
    return SourceHealth(LIMITS, **kwargs)


def test_a_recent_success_is_fresh_and_records_latency():
    health = book()
    state = health.record_success("coingecko", 42.5, now=T0)
    snap = health.snapshot(now=T0 + timedelta(seconds=10))["feeds"]["coingecko"]
    assert state == FRESH
    assert snap["state"] == FRESH
    assert snap["last_success_at"] == T0.isoformat()
    assert snap["error_count"] == 0
    assert snap["last_latency_ms"] == 42.5
    assert snap["consecutive_errors"] == 0


def test_a_feed_with_no_success_inside_the_window_is_stale():
    health = book()
    health.record_success("binance_klines", 10, now=T0)
    later = T0 + timedelta(seconds=LIMITS.stale_after_s)
    snap = health.snapshot(now=later)["feeds"]["binance_klines"]
    assert health.state_of("binance_klines", now=later) == STALE
    assert snap["state"] == STALE
    assert snap["error_count"] == 0
    assert snap["last_success_at"] == T0.isoformat()
    assert snap["last_latency_ms"] == 10.0


def test_consecutive_failures_mark_the_feed_erroring():
    health = book()
    health.record_success("coindcx", 8, now=T0)
    first = health.record_error("coindcx", error="timeout", now=T0 + timedelta(seconds=1))
    second = health.record_error("coindcx", error="timeout", now=T0 + timedelta(seconds=2))
    third = health.record_error("coindcx", error="HTTP 502", now=T0 + timedelta(seconds=3))
    snap = health.snapshot(now=T0 + timedelta(seconds=3))["feeds"]["coindcx"]
    assert first == DEGRADED
    assert second == DEGRADED
    assert third == ERRORING
    assert snap["state"] == ERRORING
    assert snap["error_count"] == 3
    assert snap["consecutive_errors"] == 3
    assert snap["last_error"] == "HTTP 502"
    assert snap["last_success_at"] == T0.isoformat()


def test_a_slow_poll_is_degraded_while_it_is_still_on_time():
    health = book()
    state = health.record_success("fear_greed", 5_001, now=T0)
    assert state == DEGRADED
    assert health.snapshot(now=T0)["feeds"]["fear_greed"]["state"] == DEGRADED


def test_a_later_success_clears_the_error_streak_and_keeps_the_count():
    health = book()
    for step in range(3):
        health.record_error("coingecko", now=T0 + timedelta(seconds=step))
    state = health.record_success("coingecko", 15, now=T0 + timedelta(seconds=4))
    snap = health.snapshot(now=T0 + timedelta(seconds=4))["feeds"]["coingecko"]
    assert state == FRESH
    assert snap["consecutive_errors"] == 0
    assert snap["error_count"] == 3
    assert snap["last_latency_ms"] == 15.0


def test_the_notify_hook_stays_off_unless_enabled():
    sent: list[str] = []
    quiet = book(notify=sent.append, notify_enabled=False)
    for step in range(3):
        quiet.record_error("coingecko", now=T0 + timedelta(seconds=step))
    assert sent == []

    loud = book(notify=sent.append, notify_enabled=True)
    loud.record_success("coingecko", 5, now=T0)
    assert sent == []
    loud.record_error("coingecko", now=T0 + timedelta(seconds=1))
    assert len(sent) == 1
    assert "coingecko" in sent[0]
    assert DEGRADED in sent[0]
    loud.record_error("coingecko", now=T0 + timedelta(seconds=2))
    assert len(sent) == 1
    loud.record_error("coingecko", now=T0 + timedelta(seconds=3))
    assert len(sent) == 2
    assert ERRORING in sent[1]
    loud.record_success("coingecko", 9, now=T0 + timedelta(seconds=4))
    assert len(sent) == 3
    assert FRESH in sent[2]


def test_a_failing_hook_does_not_hide_the_feed_state():
    def boom(_text: str) -> None:
        raise RuntimeError("notifier down")

    health = book(notify=boom, notify_enabled=True)
    assert health.record_error("coingecko", now=T0) == DEGRADED


def test_two_feeds_do_not_share_state():
    health = book()
    health.record_success("coingecko", 5, now=T0)
    health.record_error("coindcx", now=T0)
    feeds = health.snapshot(now=T0)["feeds"]
    assert feeds["coingecko"]["state"] == FRESH
    assert feeds["coindcx"]["state"] == DEGRADED


@pytest.mark.asyncio
async def test_watch_records_a_returned_failure_and_a_raised_one():
    health = book()

    async def empty():
        return 0

    async def boom():
        raise TimeoutError("slow")

    await watch("binance_klines", empty(), failed=lambda n: n == 0, health=health)
    with pytest.raises(TimeoutError):
        await watch("coingecko", boom(), failed=lambda _r: False, health=health)
    feeds = health.snapshot(now=datetime.now(UTC))["feeds"]
    assert feeds["binance_klines"]["error_count"] == 1
    assert feeds["coingecko"]["error_count"] == 1
    assert feeds["binance_klines"]["last_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_integration_health_lists_fresh_stale_and_erroring_feeds():
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler.health import make_app

    health = reset_health(book())
    try:
        now = datetime.now(UTC)
        health.record_success("coingecko", 12, now=now)
        health.record_success(
            "binance_klines", 12, now=now - timedelta(minutes=10))
        for step in range(3):
            health.record_error("coindcx", now=now + timedelta(seconds=step))

        app = await make_app(object())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/integration/v1/health")
            assert resp.status == 200
            body = await resp.json()
    finally:
        reset_health()

    feeds = body["feeds"]
    assert feeds["coingecko"]["state"] == FRESH
    assert feeds["binance_klines"]["state"] == STALE
    assert feeds["coindcx"]["state"] == ERRORING
    assert feeds["coingecko"]["last_latency_ms"] == 12.0
    assert feeds["coindcx"]["error_count"] == 3
