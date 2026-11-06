"""
Base class for news collectors (plan W7, W13).

Each collector reports health (ok / not_configured / error) without throwing from
the scheduler — the owner switches parts on from /settings (Oct 2026).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from analysis.news.sanitize import sanitize_error_message


@dataclass
class CollectorHealth:
    collector_id: str
    status: str = "idle"  # idle | ok | error | not_configured
    message: str = ""
    items_last_run: int = 0
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "collector_id": self.collector_id,
            "status": self.status,
            "message": self.message,
            "items_last_run": self.items_last_run,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
        }


@dataclass
class PollResult:
    items: int = 0
    message: str = ""


class NewsCollector:
    """One ingest source. Subclasses implement `poll` only."""

    collector_id: str = ""
    setting_key: str = ""  # e.g. news_collector_economic_calendar_enabled

    def __init__(self) -> None:
        self._health = CollectorHealth(collector_id=self.collector_id)

    @property
    def health(self) -> CollectorHealth:
        return self._health

    def _mark_ok(self, items: int, message: str = "") -> PollResult:
        now = datetime.now(UTC).replace(tzinfo=None)
        self._health.status = "ok"
        self._health.message = sanitize_error_message(message)
        self._health.items_last_run = items
        self._health.last_ok_at = now
        return PollResult(items=items, message=message)

    def _mark_not_configured(self, reason: str) -> PollResult:
        self._health.status = "not_configured"
        self._health.message = reason
        self._health.items_last_run = 0
        return PollResult(items=0, message=reason)

    def _mark_error(self, err: str) -> PollResult:
        now = datetime.now(UTC).replace(tzinfo=None)
        self._health.status = "error"
        self._health.message = sanitize_error_message(err)
        self._health.items_last_run = 0
        self._health.last_error_at = now
        return PollResult(items=0, message=self._health.message)

    async def ingest(self, session) -> int:
        """Write fetched rows to the DB. Subclasses implement."""
        raise NotImplementedError

    async def poll_and_store(self, session) -> PollResult:
        started = time.perf_counter()
        try:
            async with session.begin_nested():
                n = await self.ingest(session)
            result = self._mark_ok(n, f"stored {n} rows")
            await self._report_source(True, started, "")
            return result
        except Exception as e:
            result = self._mark_error(str(e))
            await self._report_source(False, started, result.message or str(e))
            return result

    async def poll(self) -> PollResult:
        """Health check / tests: fetch without requiring a session."""
        started = time.perf_counter()
        try:
            n = await self._fetch_count()
            result = self._mark_ok(n, f"{n} items")
            await self._report_source(True, started, "")
            return result
        except Exception as e:
            result = self._mark_error(str(e))
            await self._report_source(False, started, result.message or str(e))
            return result

    async def _report_source(self, ok: bool, started: float, error: str) -> None:
        from collectors.source_health import report_fetch
        await report_fetch(
            f"news:{self.collector_id or 'unknown'}",
            ok=ok,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
            error=error,
        )

    async def _fetch_count(self) -> int:
        raise NotImplementedError
