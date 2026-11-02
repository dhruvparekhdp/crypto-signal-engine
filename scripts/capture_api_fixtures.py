#!/usr/bin/env python3
"""Read-only GET capture for Playwright fixtures (plan §6.5). Strips key-like query params."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

ENDPOINTS = (
    "/api/status",
    "/api/crypto/coins",
    "/api/swing",
    "/api/portal/meta",
    "/api/moves?limit=24",
    "/api/events",
    "/api/golive",
)

SECRET_RE = re.compile(r"(api[_-]?key|token|secret|authorization)", re.I)


def scrub_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not SECRET_RE.search(k)]
    return urlunparse(p._replace(query=urlencode(q)))


def scrub_obj(obj):
    if isinstance(obj, dict):
        return {k: ("…" if SECRET_RE.search(k) else scrub_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(x) for x in obj]
    if isinstance(obj, str) and SECRET_RE.search(obj):
        return "…"
    return obj


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    out = Path(__file__).resolve().parents[1] / "web" / "e2e" / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    for path in ENDPOINTS:
        url = scrub_url(base.rstrip("/") + path)
        try:
            r = httpx.get(url, timeout=15)
            data = r.json() if "json" in r.headers.get("content-type", "") else {"raw": r.text[:2000]}
        except Exception as e:
            data = {"error": str(e)[:200]}
        name = path.split("?")[0].strip("/").replace("/", "_") + ".json"
        (out / name).write_text(json.dumps(scrub_obj(data), indent=2), encoding="utf-8")
        print("wrote", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
