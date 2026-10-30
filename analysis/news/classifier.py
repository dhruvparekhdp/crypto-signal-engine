"""
Classify documents: type, countries, institutions (plan W5/W7).

LLM check only for severity >= 3 via collectors/llm_client (async, optional).
"""
from __future__ import annotations

import json
import re

from analysis.news.event_types import load_event_types
from analysis.news.institutions import load_institutions
from analysis.news.severity import classify_severity


def classify_rule_based(title: str, body: str = "") -> dict:
    t = f"{title} {body}".lower()
    event_type = "other"
    if re.search(r"fomc|fed |ecb |boe |rate decision|central bank", t):
        event_type = "central_bank"
    elif re.search(r"cpi|inflation|jobs|payroll|gdp", t):
        event_type = "macro"
    elif re.search(r"treasury|deficit|debt ceiling|fiscal", t):
        event_type = "fiscal"
    elif re.search(r"war|invasion|missile|sanction", t):
        event_type = "geopolitical"
    elif re.search(r"sec |cftc|regulat|etf", t):
        event_type = "regulation"
    elif re.search(r"bitcoin|ethereum|crypto|defi|hack|exploit", t):
        event_type = "crypto_native"
    if event_type not in load_event_types():
        event_type = "other"

    countries: list[str] = []
    for c in ("US", "EU", "CN", "GB", "JP", "IN"):
        if c.lower() in t or c in title.upper():
            countries.append(c)

    institutions: list[str] = []
    for inst in load_institutions():
        for alias in [inst["code"], inst["name"], *inst.get("aliases", [])]:
            if alias and alias.lower() in t:
                institutions.append(inst["code"])
                break

    severity = classify_severity(title, event_type)
    return {
        "event_type": event_type,
        "countries": countries,
        "institutions": institutions,
        "severity": severity,
    }


async def maybe_llm_confirm_severity(title: str, severity: int, llm_call) -> int:
    """Owner plan W5: LLM only when severity >= 3."""
    if severity < 3 or llm_call is None:
        return severity
    try:
        reply = await llm_call(title)
        if isinstance(reply, dict) and "severity" in reply:
            return min(5, max(1, int(reply["severity"])))
    except Exception:
        pass
    return severity
