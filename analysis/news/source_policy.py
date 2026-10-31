"""Which collectors feed headline events vs data-only tables (plan W7)."""
from __future__ import annotations

# Documents from these sources may become news_events via the resolver.
HEADLINE_DOCUMENT_SOURCES: frozenset[str] = frozenset(
    {
        "regional_rss",
        "gdelt_15m:article",
    }
)

HEADLINE_DOCUMENT_PREFIXES: tuple[str, ...] = ("central_bank_rss:",)


def document_eligible_for_event(source_id: str) -> bool:
    if source_id in HEADLINE_DOCUMENT_SOURCES:
        return True
    return any(source_id.startswith(p) for p in HEADLINE_DOCUMENT_PREFIXES)


# Collectors that must never write news_documents (data / context only).
DATA_ONLY_COLLECTOR_IDS: frozenset[str] = frozenset(
    {
        "polymarket_odds",
        "kalshi_odds",
        "defillama",
        "cross_asset_prices",
        "us_treasury_fred",
        "binance_liquidations",
        "gdelt_15m",
    }
)
