"""Word-boundary keyword helpers (plan W19 review — no substring false positives)."""
from __future__ import annotations

import re


def boundary_pattern(alternatives: str) -> str:
    """Turn 'war|invasion' into \\bwar\\b|\\binvasion\\b; phrases keep internal spaces."""
    parts = [p.strip() for p in alternatives.split("|") if p.strip()]
    out: list[str] = []
    for p in parts:
        if " " in p:
            words = p.split()
            out.append(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b")
        else:
            out.append(rf"\b{re.escape(p)}\b")
    return "|".join(out)


def search_wb(text: str, alternatives: str) -> bool:
    return bool(re.search(boundary_pattern(alternatives), text, re.I))


def iso_country_in_text(text: str, iso2: str) -> bool:
    """Match ISO2 only as its own token (Binance must not match IN)."""
    code = iso2.upper()
    if len(code) != 2:
        return False
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(code)}(?![A-Za-z])", text))


def institution_in_text(text: str, alias: str) -> bool:
    if not alias or len(alias) < 2:
        return False
    if " " in alias:
        return search_wb(text, alias)
    return bool(re.search(rf"\b{re.escape(alias)}\b", text, re.I))
