"""Normalise raw headlines/documents (plan W7)."""
from __future__ import annotations

import hashlib
import re

from analysis.headline_dedupe import normalise as dedupe_norm


def clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip())
    return t[:2000]


def document_external_id(source_id: str, title: str, url: str = "") -> str:
    base = dedupe_norm(title) or url or title
    return hashlib.sha256(f"{source_id}:{base}".encode()).hexdigest()[:32]
