"""News engine collectors (plan W6). All default off until news_engine_enabled."""

from collectors.news.base import CollectorHealth, NewsCollector
from collectors.news.registry import COLLECTOR_IDS, collector_by_id, all_collectors

__all__ = [
    "COLLECTOR_IDS",
    "CollectorHealth",
    "NewsCollector",
    "all_collectors",
    "collector_by_id",
]
