"""Strip secrets and query strings from collector errors (plan W19)."""
from __future__ import annotations

import re


_SECRET_PARAMS = re.compile(
    r"([?&])(api_key|apikey|api_key|token|secret|password|access_key)=([^&\s\"']+)",
    re.I,
)
_URL_QUERY = re.compile(r"https?://[^\s\"']+")


def sanitize_error_message(msg: str) -> str:
    if not msg:
        return ""
    out = _SECRET_PARAMS.sub(r"\1\2=***", msg)
    out = _URL_QUERY.sub(_redact_url, out)
    return out[:500]


def _redact_url(match: re.Match) -> str:
    url = match.group(0)
    if "?" in url:
        base = url.split("?", 1)[0]
        return base + "?…"
    return url
