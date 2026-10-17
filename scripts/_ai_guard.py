"""Research/test AI jobs share the free provider quotas with production (18 Oct 2026: a bake-off and a headline
labelling run spent Groq's daily tokens and production's briefings went silent). They run only in the last hours
before Groq's 00:00 UTC reset, unless forced."""
from __future__ import annotations

import sys
import time

NIGHT_UTC = range(20, 24)          # 01:30-05:30 IST; whatever is spent comes back at 00:00 UTC


def require_quota_window(argv: list[str] | None = None) -> None:
    argv = sys.argv if argv is None else argv
    if "--force" in argv:
        return
    hour = time.gmtime().tm_hour
    if hour not in NIGHT_UTC:
        raise SystemExit(f"refused: research AI runs only {NIGHT_UTC.start}:00-24:00 UTC so production keeps its "
                         f"free quota (now {hour}:xx UTC). Add --force to run anyway.")
