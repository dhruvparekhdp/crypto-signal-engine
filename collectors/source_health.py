"""Per-feed health: last success, error count, latency, and fresh / stale / erroring.

A feed is fresh after a recent clean update, stale once that update is older than
its threshold, degraded when it is slow or only partly working, and erroring once
consecutive failures reach the threshold. The Telegram hook is installed on the
runner but sends nothing until ``source_health_notify_enabled`` is turned on.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

log = structlog.get_logger()

FRESH = "fresh"
DEGRADED = "degraded"
STALE = "stale"
ERRORING = "erroring"
UNKNOWN = "unknown"
OK = "ok"

NotifyHook = Callable[[str, str, dict], Awaitable[None]]

_registry: SourceHealth | None = None
_registry_lock = threading.Lock()


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return _aware(moment).isoformat()


def _latency(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(0.0, number)


def _round_ms(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 1)


def _clean_error(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error or "")
    text = " ".join(text.split())
    try:
        from analysis.news.sanitize import sanitize_error_message
        text = sanitize_error_message(text)
    except Exception:
        text = text[:500]
    return text[:300]


def _feed_name(name: str) -> str:
    text = str(name or "").strip()
    if not text or len(text) > 80:
        raise ValueError("feed name must be 1..80 characters")
    return text


def polls_stale_after(interval_s: float, polls: float = 3.0, floor_s: float = 30.0) -> float:
    """Stale after this many missed polls, and never sooner than ``floor_s``."""
    try:
        interval = float(interval_s)
    except (TypeError, ValueError):
        interval = floor_s
    if interval <= 0:
        interval = floor_s
    return max(floor_s, interval * polls)


def default_stale_after(name: str) -> float:
    """Three missed polls for a known source, else the configured default window."""
    try:
        from config.settings import settings
    except Exception:
        return 180.0
    key = str(name).split(":", 1)[0]
    try:
        if key == "fear_greed":
            minutes = float(getattr(settings, "fear_greed_refresh_minutes", 60) or 60)
            return polls_stale_after(minutes * 60.0)
        fixed = {"binance_oi": 120.0, "rss": 300.0, "news": 300.0}
        if key in fixed:
            return polls_stale_after(fixed[key])
        attr = {
            "binance_klines": "binance_klines_seconds",
            "delta_klines": "delta_klines_seconds",
            "coingecko": "coingecko_poll_interval_seconds",
            "coindcx": "coindcx_poll_interval_seconds",
            "cryptopanic": "cryptopanic_poll_interval_seconds",
        }.get(key)
        if attr:
            return polls_stale_after(float(getattr(settings, attr, 60) or 60))
        raw = getattr(settings, "source_health_stale_after_seconds", 180)
        return max(1.0, float(raw or 180))
    except (TypeError, ValueError):
        return 180.0


def elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)


@dataclass
class FeedHealth:
    name: str
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_latency_ms: float | None = None
    error_count: int = 0
    success_count: int = 0
    consecutive_errors: int = 0
    last_error: str = ""
    stale_after_s: float = 180.0
    partial_fail_ratio: float = 0.0
    status: str = UNKNOWN
    observations: int = 0
    last_info_log_at: datetime | None = None


class SourceHealth:
    def __init__(
        self,
        *,
        stale_after_s: float = 180.0,
        degraded_latency_ms: float = 5_000.0,
        error_threshold: int = 3,
        partial_fail_ratio: float = 0.5,
        notify_enabled: bool | None = None,
        hook: NotifyHook | None = None,
    ) -> None:
        self.stale_after_s = max(1.0, float(stale_after_s))
        self.degraded_latency_ms = max(0.0, float(degraded_latency_ms))
        self.error_threshold = max(1, int(error_threshold))
        self.partial_fail_ratio = min(1.0, max(0.0, float(partial_fail_ratio)))
        # None reads the settings flag on each publish, so the default stays off.
        self._notify_enabled = notify_enabled
        self._hook = hook
        self._feeds: dict[str, FeedHealth] = {}
        self._pending: list[dict] = []
        self._lock = threading.Lock()

    def set_hook(self, hook: NotifyHook | None) -> None:
        self._hook = hook

    def will_notify(self) -> bool:
        return self.notifications_enabled() and self._hook is not None

    def notifications_enabled(self) -> bool:
        if self._notify_enabled is not None:
            return bool(self._notify_enabled)
        try:
            from config.settings import settings
            return bool(getattr(settings, "source_health_notify_enabled", False))
        except Exception:
            return False

    def record_success(
        self,
        name: str,
        latency_ms: float | None,
        *,
        now: datetime | None = None,
        stale_after_s: float | None = None,
        failed: int = 0,
        attempted: int = 0,
        note: str = "",
        log_interval_s: float = 0.0,
    ) -> dict | None:
        moment = _aware(now)
        latency = _latency(latency_ms)
        with self._lock:
            feed = self._feed(name, stale_after_s)
            feed.observations += 1
            feed.success_count += 1
            feed.consecutive_errors = 0
            feed.last_success_at = moment
            if latency is not None:
                feed.last_latency_ms = latency
            if attempted > 0:
                feed.partial_fail_ratio = min(1.0, max(0.0, failed) / attempted)
            else:
                feed.partial_fail_ratio = 0.0
            feed.last_error = _clean_error(note) if note else ""
            change = self._apply_status(feed, moment)
            info = self._info_if_due(feed, moment, log_interval_s)
        self._emit(info, change)
        return change

    def record_error(
        self,
        name: str,
        error: BaseException | str,
        *,
        latency_ms: float | None = None,
        now: datetime | None = None,
        stale_after_s: float | None = None,
        log_interval_s: float = 0.0,
    ) -> dict | None:
        moment = _aware(now)
        latency = _latency(latency_ms)
        with self._lock:
            feed = self._feed(name, stale_after_s)
            feed.observations += 1
            feed.error_count += 1
            feed.consecutive_errors += 1
            feed.last_error_at = moment
            feed.last_error = _clean_error(error) or "fetch failed"
            if latency is not None:
                feed.last_latency_ms = latency
            change = self._apply_status(feed, moment)
            info = self._info_if_due(feed, moment, log_interval_s)
        self._emit(info, change)
        return change

    def snapshot(self, now: datetime | None = None) -> dict:
        """Recompute statuses. Time alone can move a feed from fresh to stale."""
        moment = _aware(now)
        changes: list[dict] = []
        with self._lock:
            for feed in self._feeds.values():
                if feed.observations == 0:
                    continue
                change = self._apply_status(feed, moment)
                if change is not None:
                    changes.append(change)
            feeds = [self._public(feed) for feed in self._feeds.values() if feed.observations]
            notify = self.notifications_enabled()
            thresholds = {
                "stale_after_seconds": self.stale_after_s,
                "degraded_latency_ms": self.degraded_latency_ms,
                "error_threshold": self.error_threshold,
                "partial_fail_ratio": self.partial_fail_ratio,
            }
        for change in changes:
            self._emit(None, change)
        feeds.sort(key=lambda row: row["name"])
        return {
            "status": _overall([row["status"] for row in feeds]),
            "notify_enabled": notify,
            "feeds": feeds,
            "thresholds": thresholds,
        }

    async def publish_pending(self) -> None:
        """Send status transitions. Drops them when the hook is off."""
        with self._lock:
            notes = self._pending
            self._pending = []
            hook = self._hook
            enabled = self.notifications_enabled()
        if not notes or not enabled or hook is None:
            return
        for note in notes:
            try:
                await hook(note["feed"], note["status"], note)
            except Exception as exc:
                log.warning(
                    "source_health_notify_failed",
                    feed=note.get("feed"),
                    error=type(exc).__name__,
                )

    def schedule_publish(self) -> None:
        """Notify without making the collector wait on Telegram."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.publish_pending())

    def _feed(self, name: str, stale_after_s: float | None) -> FeedHealth:
        key = _feed_name(name)
        feed = self._feeds.get(key)
        if feed is None:
            window = self.stale_after_s if stale_after_s is None else max(1.0, float(stale_after_s))
            feed = FeedHealth(name=key, stale_after_s=window)
            self._feeds[key] = feed
        elif stale_after_s is not None:
            feed.stale_after_s = max(1.0, float(stale_after_s))
        return feed

    def _status(self, feed: FeedHealth, now: datetime) -> str:
        if feed.observations == 0:
            return UNKNOWN
        if feed.consecutive_errors >= self.error_threshold:
            return ERRORING
        if feed.last_success_at is None:
            return DEGRADED
        age = (now - feed.last_success_at).total_seconds()
        if age > feed.stale_after_s:
            return STALE
        slow = (
            feed.last_latency_ms is not None
            and feed.last_latency_ms > self.degraded_latency_ms
        )
        partial = feed.partial_fail_ratio > 0 and feed.partial_fail_ratio >= self.partial_fail_ratio
        if slow or partial or feed.consecutive_errors > 0:
            return DEGRADED
        return FRESH

    def _apply_status(self, feed: FeedHealth, now: datetime) -> dict | None:
        previous = feed.status
        new = self._status(feed, now)
        feed.status = new
        if previous == new:
            return None
        note = self._alert_note(feed, previous, new)
        if note is None:
            return None
        self._pending.append(note)
        return {
            "feed": feed.name,
            "status": new,
            "previous": previous,
            "error_count": feed.error_count,
            "latency_ms": _round_ms(feed.last_latency_ms),
            "last_success_at": _iso(feed.last_success_at),
        }

    def _alert_note(self, feed: FeedHealth, previous: str, new: str) -> dict | None:
        entered_bad = new in (DEGRADED, STALE, ERRORING)
        recovered = new == FRESH and previous in (DEGRADED, STALE, ERRORING)
        if not entered_bad and not recovered:
            return None
        return {
            "feed": feed.name,
            "status": new,
            "previous": previous,
            "error_count": feed.error_count,
            "consecutive_errors": feed.consecutive_errors,
            "last_latency_ms": _round_ms(feed.last_latency_ms),
            "last_success_at": _iso(feed.last_success_at),
            "last_error": feed.last_error,
        }

    def _info_if_due(self, feed: FeedHealth, now: datetime, log_interval_s: float) -> dict | None:
        if log_interval_s > 0 and feed.last_info_log_at is not None:
            elapsed = (now - feed.last_info_log_at).total_seconds()
            if elapsed < log_interval_s:
                return None
        feed.last_info_log_at = now
        return {
            "feed": feed.name,
            "status": feed.status,
            "last_success_at": _iso(feed.last_success_at),
            "error_count": feed.error_count,
            "latency_ms": _round_ms(feed.last_latency_ms),
            "consecutive_errors": feed.consecutive_errors,
        }

    def _public(self, feed: FeedHealth) -> dict:
        return {
            "name": feed.name,
            "status": feed.status,
            "last_success_at": _iso(feed.last_success_at),
            "last_error_at": _iso(feed.last_error_at),
            "error_count": feed.error_count,
            "consecutive_errors": feed.consecutive_errors,
            "success_count": feed.success_count,
            "last_latency_ms": _round_ms(feed.last_latency_ms),
            "last_error": feed.last_error,
            "stale_after_seconds": feed.stale_after_s,
        }

    def _emit(self, info: dict | None, change: dict | None) -> None:
        if info is not None:
            log.info("source_health", **info)
        if change is None:
            return
        logger = log.warning if change["status"] in (DEGRADED, STALE, ERRORING) else log.info
        logger("source_health_changed", **change)


def _overall(statuses: list[str]) -> str:
    if any(status == ERRORING for status in statuses):
        return ERRORING
    if any(status in (DEGRADED, STALE) for status in statuses):
        return DEGRADED
    return OK


def get_registry() -> SourceHealth:
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = _registry_from_settings()
        return _registry


def reset_for_tests(reg: SourceHealth | None = None) -> SourceHealth:
    """Replace the process registry. Tests only."""
    global _registry
    with _registry_lock:
        _registry = reg if reg is not None else SourceHealth(notify_enabled=False)
        return _registry


def _registry_from_settings() -> SourceHealth:
    try:
        from config.settings import settings
        stale = float(getattr(settings, "source_health_stale_after_seconds", 180) or 180)
        latency = float(getattr(settings, "source_health_degraded_latency_ms", 5000) or 5000)
        errors = int(getattr(settings, "source_health_error_threshold", 3) or 3)
        partial = float(getattr(settings, "source_health_partial_fail_ratio", 0.5))
    except Exception:
        stale, latency, errors, partial = 180.0, 5000.0, 3, 0.5
    return SourceHealth(
        stale_after_s=stale,
        degraded_latency_ms=latency,
        error_threshold=errors,
        partial_fail_ratio=partial,
    )


async def report_fetch(
    name: str,
    *,
    ok: bool,
    latency_ms: float | None,
    error: str = "",
    failed: int = 0,
    attempted: int = 0,
    note: str = "",
    stale_after_s: float | None = None,
    log_interval_s: float = 0.0,
    now: datetime | None = None,
) -> None:
    """Record one fetch. Never raises: health tracking must not take a feed down."""
    try:
        reg = get_registry()
        window = default_stale_after(name) if stale_after_s is None else stale_after_s
        if ok:
            reg.record_success(
                name, latency_ms, now=now, stale_after_s=window, failed=failed,
                attempted=attempted, note=note, log_interval_s=log_interval_s,
            )
        else:
            reg.record_error(
                name, error or note or "fetch failed", latency_ms=latency_ms, now=now,
                stale_after_s=window, log_interval_s=log_interval_s,
            )
        if reg.will_notify():
            reg.schedule_publish()
        else:
            await reg.publish_pending()
    except Exception as exc:
        log.warning("source_health_record_failed", feed=str(name)[:80], error=type(exc).__name__)


@asynccontextmanager
async def watching(name: str):
    """Time one fetch. Set ``outcome['ok']`` before leaving; an exception is an error.

    The exception still propagates. Swallowed collector errors should set
    ``outcome['error']`` and return, which is what those collectors already do.
    """
    started = time.perf_counter()
    outcome: dict = {"ok": False, "error": "", "failed": 0, "attempted": 0, "note": ""}
    try:
        yield outcome
    except Exception as exc:
        outcome["ok"] = False
        if not outcome["error"]:
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if not outcome["ok"] and not outcome["error"]:
            outcome["error"] = "no data"
        await report_fetch(
            name,
            ok=bool(outcome["ok"]),
            latency_ms=elapsed_ms(started),
            error=str(outcome["error"]),
            failed=int(outcome["failed"] or 0),
            attempted=int(outcome["attempted"] or 0),
            note=str(outcome["note"] or ""),
        )
