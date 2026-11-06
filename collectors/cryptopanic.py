from __future__ import annotations

import httpx
import structlog

from analysis.crypto_state import NewsItem
from config.settings import settings

log = structlog.get_logger()


class CryptoPanicCollector:
    """Fetches real-time crypto news, market catalysts, and community sentiment votes from CryptoPanic API."""

    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self) -> None:
        self._consecutive_failures = 0

    async def fetch(self) -> list[NewsItem]:
        if not settings.cryptopanic_auth_token:
            log.debug("cryptopanic_skipped_no_token")
            return []

        from collectors.source_health import watching

        async with watching("cryptopanic") as outcome:
            return await self._fetch_watched(outcome)

    async def _fetch_watched(self, outcome: dict) -> list[NewsItem]:
        params = {
            "auth_token": settings.cryptopanic_auth_token,
            "kind": "news",
            "filter": "rising",
            "public": "true",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(self.BASE_URL, params=params)

                if resp.status_code != 200:
                    log.warning("cryptopanic_http_error", status=resp.status_code, body=resp.text[:200])
                    self._consecutive_failures += 1
                    outcome["error"] = f"HTTP {resp.status_code}"
                    return []

                data = resp.json()
                results = data.get("results", [])
                items = [NewsItem.from_api(post) for post in results]
                self._consecutive_failures = 0
                log.info("cryptopanic_news_fetched", count=len(items))
                outcome["ok"] = True
                outcome["attempted"] = len(items)
                return items

        except Exception as exc:
            self._consecutive_failures += 1
            log.warning("cryptopanic_fetch_failed", error=str(exc))
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            return []
