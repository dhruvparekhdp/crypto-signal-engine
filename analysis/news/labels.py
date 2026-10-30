"""Attach news context labels to signals / candidates (W9 — labels only)."""


def format_event_label(event_type: str, severity: int, title: str) -> str:
    t = (title or "").strip()
    short = t[:60] + ("…" if len(t) > 60 else "")
    return f"news:{event_type}:L{severity}:{short}"
