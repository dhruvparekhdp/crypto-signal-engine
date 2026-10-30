"""HTTP helper — fixtures in tests, httpx in production (plan W15)."""
from __future__ import annotations

from typing import Any

import httpx

from collectors.news.fixtures import load_fixture, load_fixture_text, use_fixtures


async def fetch_json(url: str, *, fixture: str, headers: dict | None = None, timeout: float = 30.0) -> Any:
    if use_fixtures():
        return load_fixture(fixture)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()
        return resp.json()


async def fetch_text(url: str, *, fixture: str, headers: dict | None = None, timeout: float = 30.0) -> str:
    if use_fixtures():
        return load_fixture_text(fixture)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()
        return resp.text
