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


# ── same story, rewritten headline (owner 3 Nov 2026: "many of the events are showing duplicate") ─────────────────────
# BBC rewrites a story's headline as it develops ("kills 18" -> "kills 24", "from London" -> "from Luton"); the event
# key matched exact titles only. Two headlines are one story when, ignoring small words and treating every number as
# the same token, most of their words match.
import re as _re

_STOP = frozenset("a an the in on at of to for and or by with from as is are was were be after before over into "
                  "its it this that what how why who says said say about amid".split())
SAME_STORY_JACCARD = 0.5
SAME_STORY_OVERLAP = 0.7


def story_tokens(title: str) -> frozenset[str]:
    words = _re.findall(r"[a-z0-9]+", (title or "").lower().replace("\u2019", "'").replace("'", ""))
    return frozenset("#" if w.isdigit() else w for w in words if w not in _STOP and (len(w) > 1 or w.isdigit()))


def same_story(a: str, b: str) -> bool:
    ta, tb = story_tokens(a), story_tokens(b)
    if len(ta) < 3 or len(tb) < 3:
        return False
    shared = len(ta & tb)
    return (shared / len(ta | tb) >= SAME_STORY_JACCARD
            or (shared >= 4 and shared / min(len(ta), len(tb)) >= SAME_STORY_OVERLAP))
