"""
Classify documents: type, countries, institutions (plan W5/W7).

LLM check only for severity >= 3 via collectors/llm_client (async, optional).
"""
from __future__ import annotations

import re

from analysis.news.countries import load_countries
from analysis.news.event_types import load_event_types
from analysis.news.institutions import load_institutions
from analysis.news.matchers import institution_in_text, iso_country_in_text, search_wb
from analysis.news.severity import classify_severity


def classify_rule_based(title: str, body: str = "") -> dict:
    t = f"{title} {body}".lower()
    event_type = "other"
    if search_wb(t, "fomc|fed|ecb|boe|boj|pboc|rate decision|central bank|mpc decision"):
        event_type = "central_bank"
    elif search_wb(t, "cpi|inflation|jobs report|nonfarm|payrolls|gdp"):
        event_type = "macro"
    elif search_wb(t, "treasury|deficit|debt ceiling|fiscal"):
        event_type = "fiscal"
    elif search_wb(t, "war|invasion|missile|sanction"):
        event_type = "geopolitical"
    elif search_wb(t, "sec|cftc|regulat|etf"):
        event_type = "regulation"
    elif search_wb(t, "bitcoin|ethereum|crypto|defi|hack|exploit|binance"):
        event_type = "crypto_native"
    if event_type not in load_event_types():
        event_type = "other"

    countries: list[str] = []
    for c in load_countries():
        iso = c.get("iso2", "")
        name = c.get("name", "")
        if iso and iso_country_in_text(f"{title} {body}", iso):
            countries.append(iso)
        elif name and search_wb(t, name):
            countries.append(iso)

    institutions: list[str] = []
    for inst in load_institutions():
        for alias in [inst["code"], inst["name"], *inst.get("aliases", [])]:
            if alias and institution_in_text(t, alias):
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
