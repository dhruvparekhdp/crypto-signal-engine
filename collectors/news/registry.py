"""Registry of news collectors and their settings keys (plan W6)."""
from __future__ import annotations

from collectors.news.base import NewsCollector

COLLECTOR_IDS: tuple[str, ...] = (
    "economic_calendar",
    "central_bank_rss",
    "us_treasury_fred",
    "polymarket_odds",
    "kalshi_odds",
    "binance_liquidations",
    "defillama",
    "cross_asset_prices",
    "regional_rss",
    "gdelt_15m",
)

_SETTING_SUFFIX = "_enabled"


def setting_key_for(collector_id: str) -> str:
    return f"news_collector_{collector_id}{_SETTING_SUFFIX}"


def all_collectors() -> list[NewsCollector]:
    try:
        from collectors.news import sources  # noqa: WPS433 — lazy import avoids cycles

        return list(sources.build_collectors())
    except ImportError:
        return []


def collector_by_id(collector_id: str) -> NewsCollector | None:
    for c in all_collectors():
        if c.collector_id == collector_id:
            return c
    return None
