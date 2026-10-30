"""World news + sentiment engine (plan W7–W14, Oct 2026). Paper labels and research only."""

from analysis.news.event_types import EVENT_TYPES, event_type_severity_floor, load_event_types
from analysis.news.severity import classify_severity, load_severity_rules

__all__ = [
    "EVENT_TYPES",
    "classify_severity",
    "event_type_severity_floor",
    "load_event_types",
    "load_severity_rules",
]
