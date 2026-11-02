"""Slim /api/moves payloads (UI rebuild plan §2 0b)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_result(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def run_detail_row(row) -> dict:
    """One attribution run with full bodies (moves page detail view)."""
    return {
        "id": row.id,
        "at": _iso(row.created_at),
        "window_hours": row.window_hours,
        "model": row.model,
        "latency_ms": row.latency_ms,
        "moves": json.loads(row.moves or "[]"),
        "signals": json.loads(row.signals or "[]"),
        "result": _parse_result(row.result),
    }


def run_summary_row(row) -> dict:
    """History list entry — no moves/signals/full coin bodies."""
    result = _parse_result(row.result)
    overall = (result.get("overall") or "").strip()
    preview = overall.split(". ")[0] if overall else ""
    coins = result.get("coins") or []
    return {
        "id": row.id,
        "at": _iso(row.created_at),
        "window_hours": row.window_hours,
        "model": row.model,
        "latency_ms": row.latency_ms,
        "overall_preview": preview,
        "news_driven": sum(1 for c in coins if c.get("cause_type") == "news"),
    }


def moves_shared_shell(*, briefing, events, shadow, upcoming, monitor, news_store=None, news_store_active=None) -> dict:
    body: dict[str, Any] = {
        "briefing": None if briefing is None else {
            "at": _iso(briefing.created_at),
            "risk_tone": briefing.risk_tone,
            "summary": briefing.summary,
            "events": json.loads(briefing.events or "[]"),
            "model": briefing.model,
        },
        "upcoming": upcoming,
        "monitor": monitor,
        "events": events,
        "shadow": shadow,
    }
    if news_store is not None:
        body["news_store"] = news_store
    if news_store_active is not None:
        body["news_store_active"] = news_store_active
    return body
