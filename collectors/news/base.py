"""
Base class for news collectors (plan W7, W13).

Each collector reports health (ok / not_configured / error) without throwing from
the scheduler — the owner switches parts on from /settings (Oct 2026).
"""
from __future__ import annotations

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
        try:
            n = await self.ingest(session)
            return self._mark_ok(n, f"stored {n} rows")
        except Exception as e:
            return self._mark_error(str(e))

    async def poll(self) -> PollResult:
        """Health check / tests: fetch without requiring a session."""
        try:
            n = await self._fetch_count()
            return self._mark_ok(n, f"{n} items")
        except Exception as e:
            return self._mark_error(str(e))

    async def _fetch_count(self) -> int:
        raise NotImplementedError
