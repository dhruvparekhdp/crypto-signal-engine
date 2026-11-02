"""Pagination and field projection for /api/signals/history."""
from __future__ import annotations

ALLOWED_SIGNAL_FIELDS = frozenset(
    {
        "id", "symbol", "signal_type", "direction", "confidence", "current_price",
        "target_price", "stop_loss", "edge_pct", "timeframe", "outcome", "pnl_pct",
        "timestamp", "trade_mode", "veto_reason", "role",
    }
)


def project_signal_row(row_dict: dict, fields: str | None) -> dict:
    if not fields:
        return row_dict
    keep = {p.strip() for p in fields.split(",") if p.strip()}
    if not keep:
        return row_dict
    unknown = keep - ALLOWED_SIGNAL_FIELDS
    if unknown:
        keep -= unknown
    return {k: v for k, v in row_dict.items() if k in keep}


def history_limit(query, *, default: int = 200) -> int:
    try:
        n = int(query.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(3000, n))


def parse_cursor(query) -> int | None:
    if "cursor" not in query:
        return None
    raw = (query.get("cursor") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def uses_paginated_envelope(query) -> bool:
    return "cursor" in query
