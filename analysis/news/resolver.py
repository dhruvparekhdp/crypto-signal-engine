"""One canonical event per real-world story (plan W7)."""
from __future__ import annotations

import hashlib
from datetime import datetime

from analysis.headline_dedupe import normalise


def event_key_for(title: str, event_type: str, day: datetime | None = None) -> str:
    d = (day or datetime.utcnow()).strftime("%Y-%m-%d")
    norm = normalise(title)[:120]
    return hashlib.sha256(f"{event_type}:{d}:{norm}".encode()).hexdigest()[:24]


def is_followup(title: str, prior_title: str) -> bool:
    a, b = normalise(title), normalise(prior_title)
    if not a or not b:
        return False
    return a.startswith(b[:40]) or b.startswith(a[:40])
