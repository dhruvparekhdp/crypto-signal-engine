"""
Link live impulses (data/impulse_events.jsonl, W18) to open news events — no change to impulse.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


def impulse_row_key(row: dict) -> str:
    sym = row.get("symbol", "")
    at_ms = row.get("at_ms") or row.get("at") or 0
    side = row.get("side", 0)
    return f"{sym}:{at_ms}:{side}"


def parse_impulse_jsonl(path: Path, *, limit: int = 500) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return rows


def events_open_at(events: list[dict], at: datetime, *, window_min: int = 60) -> list[int]:
    """events: {id, started_at iso, status}"""
    out = []
    for ev in events:
        if ev.get("status") != "open":
            continue
        try:
            start = datetime.fromisoformat(ev["started_at"])
        except (TypeError, ValueError, KeyError):
            continue
        if start <= at <= start + timedelta(minutes=window_min * 24):
            out.append(int(ev["id"]))
    return out
