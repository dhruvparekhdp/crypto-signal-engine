"""Event memory windows before auto-resolve (PR #45 follow-up)."""
from __future__ import annotations

from datetime import timedelta


def memory_window_for_severity(severity: int) -> timedelta:
    """L1-2: 6h, L3: 2d, L4-5: 7d."""
    sev = max(1, min(5, int(severity)))
    if sev <= 2:
        return timedelta(hours=6)
    if sev == 3:
        return timedelta(days=2)
    return timedelta(days=7)
