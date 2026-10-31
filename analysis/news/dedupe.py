"""Dedupe using analysis/headline_dedupe (plan W3, W19)."""
from __future__ import annotations

from datetime import datetime

from analysis.headline_dedupe import RecentHeadlines, mark_duplicates


def mark_doc_duplicates(
    docs: list[dict],
    *,
    threshold: float = 0.90,
    window_hours: float = 12.0,
) -> list[dict]:
    """Each doc: external_id, title, published_at (datetime). Sets duplicate_of when matched."""
    recent = RecentHeadlines(threshold, window_hours)
    now = datetime.utcnow()
    headlines = []
    for d in docs:
        h = type("_H", (), {})()
        h.external_id = d["external_id"]
        h.headline = d.get("title") or ""
        h.published_at = d.get("published_at") or now
        h.duplicate_of = ""
        headlines.append(h)
    mark_duplicates(headlines, None, recent, now)
    out = []
    for h, d in zip(headlines, docs):
        row = dict(d)
        row["duplicate_of"] = h.duplicate_of
        out.append(row)
    return out
