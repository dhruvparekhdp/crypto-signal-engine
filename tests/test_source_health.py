"""Data-source health: fresh, stale, and erroring feeds, plus the notify hook."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from structlog.testing import capture_logs

from collectors.source_health import (
    SourceHealth,
    get_registry,
    reset_for_tests,
    watching,
)


def _clock(seconds: float = 0) -> datetime:
    return datetime(2026, 11, 6, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _feed(snapshot: dict, name: str) -> dict:
    return next(row for row in snapshot["feeds"] if row["name"] == name)


@pytest.fixture
def registry():
    reg = SourceHealth(
        stale_after_s=60,
        degraded_latency_ms=1000,
        error_threshold=3,
        partial_fail_ratio=0.5,
        notify_enabled=False,
    )
    reset_for_tests(reg)
    yield reg
    reset_for_tests(SourceHealth(notify_enabled=False))


def test_the_notify_setting_defaults_to_off():
    from config.settings import Settings
    assert Settings.model_fields["source_health_notify_enabled"].default is False


def test_a_recent_clean_update_is_fresh(registry):
    now = _clock()
    registry.record_success("binance_klines", 40, now=now, stale_after_s=60)
    snap = registry.snapshot(now=now)
    feed = _feed(snap, "binance_klines")
    assert feed["status"] == "fresh"
    assert feed["error_count"] == 0
    assert feed["consecutive_errors"] == 0
    assert feed["success_count"] == 1
    assert feed["last_latency_ms"] == 40.0
    assert feed["last_success_at"] == now.isoformat()
    assert snap["status"] == "ok"
    assert snap["notify_enabled"] is False


def test_a_feed_goes_stale_once_its_threshold_passes(registry):
    registry.record_success("delta_klines", 20, now=_clock(), stale_after_s=60)
    with capture_logs() as captured:
        snap = registry.snapshot(now=_clock(61))
    feed = _feed(snap, "delta_klines")
    assert feed["status"] == "stale"
    assert feed["last_success_at"] == _clock().isoformat()
    assert snap["status"] == "degraded"
    changed = [row for row in captured if row["event"] == "source_health_changed"]
    assert changed[0]["status"] == "stale"
    assert changed[0]["feed"] == "delta_klines"
    again = registry.snapshot(now=_clock(120))
    assert _feed(again, "delta_klines")["status"] == "stale"


def test_repeated_errors_mark_the_feed_erroring(registry):
    for i in range(3):
        registry.record_error("coingecko", "HTTP 503", latency_ms=15, now=_clock(i))
    snap = registry.snapshot(now=_clock(3))
    feed = _feed(snap, "coingecko")
    assert feed["status"] == "erroring"
    assert feed["error_count"] == 3
    assert feed["consecutive_errors"] == 3
    assert feed["last_success_at"] is None
    assert feed["last_latency_ms"] == 15.0
    assert "503" in feed["last_error"]
    assert snap["status"] == "erroring"


def test_one_error_after_a_fresh_update_is_degraded(registry):
    registry.record_success("coindcx", 30, now=_clock(), stale_after_s=60)
    registry.record_error("coindcx", "timeout", latency_ms=30, now=_clock(1))
    snap = registry.snapshot(now=_clock(1))
    assert _feed(snap, "coindcx")["status"] == "degraded"
    assert _feed(snap, "coindcx")["error_count"] == 1
    assert snap["status"] == "degraded"


def test_a_slow_update_and_a_partial_update_are_degraded(registry):
    registry.record_success("binance_oi", 1500, now=_clock(), stale_after_s=60)
    assert _feed(registry.snapshot(now=_clock()), "binance_oi")["status"] == "degraded"
    registry.record_success(
        "delta_klines", 20, now=_clock(), stale_after_s=60, failed=3, attempted=4,
    )
    assert _feed(registry.snapshot(now=_clock()), "delta_klines")["status"] == "degraded"


def test_a_later_clean_update_clears_an_erroring_feed(registry):
    for i in range(3):
        registry.record_error("fear_greed", "HTTP 500", now=_clock(i))
    registry.record_success("fear_greed", 80, now=_clock(4), stale_after_s=60)
    feed = _feed(registry.snapshot(now=_clock(4)), "fear_greed")
    assert feed["status"] == "fresh"
    assert feed["consecutive_errors"] == 0
    assert feed["error_count"] == 3
    assert feed["success_count"] == 1


def test_updates_are_logged_with_success_time_errors_and_latency(registry):
    now = _clock()
    with capture_logs() as captured:
        registry.record_success("binance_klines", 12.5, now=now)
    logged = [row for row in captured if row["event"] == "source_health"]
    assert logged[0]["feed"] == "binance_klines"
    assert logged[0]["status"] == "fresh"
    assert logged[0]["last_success_at"] == now.isoformat()
    assert logged[0]["error_count"] == 0
    assert logged[0]["latency_ms"] == 12.5


def test_error_text_does_not_keep_query_secrets(registry):
    registry.record_error(
        "cryptopanic",
        "boom https://cryptopanic.com/api/v1/posts/?auth_token=supersecret&public=true",
        now=_clock(),
    )
    feed = _feed(registry.snapshot(now=_clock()), "cryptopanic")
    assert "supersecret" not in feed["last_error"]
    assert "auth_token" not in feed["last_error"]


async def test_the_hook_stays_quiet_until_it_is_enabled():
    sent: list[str] = []

    async def hook(feed, status, detail):
        sent.append(status)

    quiet = SourceHealth(error_threshold=1, notify_enabled=False, hook=hook, stale_after_s=60)
    quiet.record_error("delta_klines", "down", now=_clock())
    await quiet.publish_pending()
    assert sent == []

    loud = SourceHealth(error_threshold=1, stale_after_s=60, notify_enabled=True, hook=hook)
    loud.record_error("delta_klines", "down", latency_ms=9, now=_clock())
    await loud.publish_pending()
    assert sent == ["erroring"]
    loud.record_error("delta_klines", "down", now=_clock(1))
    await loud.publish_pending()
    assert sent == ["erroring"]
    loud.record_success("delta_klines", 10, now=_clock(2))
    await loud.publish_pending()
    assert sent == ["erroring", "fresh"]


async def test_stale_notifies_once_when_enabled():
    sent: list[tuple[str, str]] = []

    async def hook(feed, status, detail):
        sent.append((feed, status))
        assert detail["error_count"] == 0
        assert detail["last_success_at"] == _clock().isoformat()

    health = SourceHealth(stale_after_s=30, notify_enabled=True, hook=hook)
    health.record_success("delta_klines", 10, now=_clock(), stale_after_s=30)
    await health.publish_pending()
    assert sent == []
    health.snapshot(now=_clock(31))
    await health.publish_pending()
    assert sent == [("delta_klines", "stale")]
    health.snapshot(now=_clock(90))
    await health.publish_pending()
    assert sent == [("delta_klines", "stale")]


async def test_a_raised_fetch_is_recorded_and_still_raises(registry):
    class Boom:
        async def get_symbols(self):
            raise RuntimeError("db down")

    from collectors.binance_klines import BinanceKlines
    with pytest.raises(RuntimeError, match="db down"):
        await BinanceKlines(Boom()).fetch()
    feed = _feed(get_registry().snapshot(), "binance_klines")
    assert feed["error_count"] == 1
    assert "db down" in feed["last_error"]
    assert feed["status"] == "degraded"


async def test_an_empty_watchlist_is_a_fresh_candle_feed(registry):
    class Empty:
        async def get_symbols(self):
            return []

    from collectors.binance_klines import BinanceKlines
    assert await BinanceKlines(Empty()).fetch() == 0
    feed = _feed(get_registry().snapshot(), "binance_klines")
    assert feed["status"] == "fresh"
    assert feed["success_count"] == 1


async def test_fear_greed_http_failure_is_recorded(registry):
    registry.error_threshold = 1

    class Response:
        status_code = 503

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return Response()

    monkey = pytest.MonkeyPatch()
    monkey.setattr("collectors.sentiment_feeds.httpx.AsyncClient", Client)
    try:
        from collectors.sentiment_feeds import fetch_fear_greed
        assert await fetch_fear_greed() is None
    finally:
        monkey.undo()
    feed = _feed(get_registry().snapshot(), "fear_greed")
    assert feed["status"] == "erroring"
    assert feed["last_error"] == "HTTP 503"


async def test_watching_reraises_after_recording(registry):
    with pytest.raises(RuntimeError, match="timeout"):
        async with watching("coingecko"):
            raise RuntimeError("timeout")
    feed = _feed(get_registry().snapshot(), "coingecko")
    assert "timeout" in feed["last_error"]
    assert feed["error_count"] == 1


async def test_health_routes_report_fresh_and_erroring_feeds(registry):
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler.health import make_app
    from tests.test_api_security import FakeRunner

    # The route snapshots with the real clock, so this success has to be recent.
    now = datetime.now(UTC)
    registry.record_success("binance_klines", 25, now=now, stale_after_s=3600)
    for _ in range(3):
        registry.record_error("coingecko", "HTTP 503", latency_ms=11, now=now)

    app = await make_app(FakeRunner())
    async with TestClient(TestServer(app)) as client:
        integration = await client.get("/integration/v1/health")
        assert integration.status == 200
        body = await integration.json()
        assert body["status"] == "erroring"
        assert body["notify_enabled"] is False
        assert _feed(body, "binance_klines")["status"] == "fresh"
        assert _feed(body, "binance_klines")["last_latency_ms"] == 25.0
        assert _feed(body, "coingecko")["status"] == "erroring"
        assert _feed(body, "coingecko")["error_count"] == 3

        liveness = await client.get("/health")
        assert liveness.status == 200
        live = await liveness.json()
        assert live["status"] == "ok"
        assert live["symbols_tracked"] == 0
        assert live["data_sources"]["status"] == "erroring"
        assert _feed(live["data_sources"], "coingecko")["error_count"] == 3


async def test_the_runner_hook_uses_the_notifier_and_stays_off(monkeypatch):
    from config.settings import settings
    from scheduler.runner import AppRunner

    monkeypatch.setattr(settings, "source_health_notify_enabled", False)
    runner = AppRunner()
    sent: list[str] = []

    async def send_text(text, parse_mode=None):
        sent.append(text)
        return True

    runner.notifier.send_text = send_text
    detail = {
        "error_count": 2,
        "last_latency_ms": 40.0,
        "last_success_at": None,
        "last_error": "HTTP 503",
    }
    await runner._notify_source_health("delta_klines", "stale", detail)
    assert sent == []
    monkeypatch.setattr(settings, "source_health_notify_enabled", True)
    await runner._notify_source_health("delta_klines", "stale", detail)
    assert len(sent) == 1
    assert "delta_klines" in sent[0]
    assert "stale" in sent[0]
    assert "errors=2" in sent[0]
    assert "HTTP 503" in sent[0]
    reset_for_tests(SourceHealth(notify_enabled=False))
