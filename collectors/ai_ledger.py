"""
AI quality ledger (owner, 18 Oct 2026, AI plan step 4): one small line per AI outcome, so the System > AI tab can show
for every job whether the AI answered, who answered, how fast, what failed and why, and how often an answer had to be
thrown away (e.g. an event dated in the future). Some days the AI looked good and some days not; this makes it a number.

    data/ai_ledger/YYYY-MM-DD.jsonl   {"ts", "role", "outcome", "provider", "model", "ms", "detail"}

outcome: ok | no_answer (the whole chain failed) | budget_skip | circuit_open | not_json | error | rejected_future
Never a prompt, never an answer, never a key (error text is scrubbed). Writing never raises.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path("data/ai_ledger")
FAILURE_KINDS = ("budget_skip", "circuit_open", "not_json", "error")
_SECRET = re.compile(r"(key=|api[_-]?key[\"']?\s*[:=]\s*[\"']?|bearer\s+|token[\"']?\s*[:=]\s*[\"']?)[^\s&\"',]+", re.I)


def scrub(text: str) -> str:
    """Error text without anything that looks like a credential."""
    return _SECRET.sub(lambda m: m.group(1) + "REDACTED", str(text or ""))[:200]


def note(role: str, outcome: str, provider: str = "", model: str = "", ms: int = 0, detail: str = "",
         n: int = 1, root: Path | None = None, now: datetime | None = None) -> None:
    """Record one outcome (n > 1 for a batch, e.g. three rejected events in one briefing)."""
    try:
        now = now or datetime.now(UTC)
        d = root or ROOT
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now.isoformat(timespec="seconds"), "role": role, "outcome": outcome, "provider": provider,
               "model": model, "ms": int(ms), "detail": scrub(detail)}
        if n != 1:
            rec["n"] = int(n)
        with open(d / f"{now:%Y-%m-%d}.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - measuring must never break the AI call it measures
        pass


def _read(days: int, root: Path, now: datetime) -> list[dict]:
    out = []
    for i in range(days):
        p = root / f"{(now - timedelta(days=i)):%Y-%m-%d}.jsonl"
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def _p90(xs: list[int]) -> int | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def summary(days: int = 7, root: Path | None = None, now: datetime | None = None) -> dict:
    """Per job and per provider: answered / failed / rejected, speed, who answered; plus the latest problems."""
    now = now or datetime.now(UTC)
    rows = _read(max(1, days), root or ROOT, now)
    today = f"{now:%Y-%m-%d}"
    roles: dict[str, dict] = defaultdict(lambda: {"answered": 0, "no_answer": 0, "fallbacks": 0, "rejected": 0,
                                                  "answered_today": 0, "no_answer_today": 0, "ms": [],
                                                  "served_by": Counter(), "failures": Counter()})
    providers: dict[str, Counter] = defaultdict(Counter)
    problems = []
    for r in rows:
        role, out, n = r.get("role") or "?", r.get("outcome"), int(r.get("n") or 1)
        who = f"{r.get('provider')}/{r.get('model')}".strip("/")
        x = roles[role]
        if out == "ok":
            x["answered"] += n
            x["answered_today"] += n if r.get("ts", "").startswith(today) else 0
            x["ms"].append(int(r.get("ms") or 0))
            x["served_by"][who] += n
            x["fallbacks"] += n if "fallback" in (r.get("detail") or "") else 0
            providers[who]["ok"] += n
        elif out == "no_answer":
            x["no_answer"] += n
            x["no_answer_today"] += n if r.get("ts", "").startswith(today) else 0
        elif out == "rejected_future":
            x["rejected"] += n
        elif out in FAILURE_KINDS:
            x["failures"][out] += n
            providers[who][out] += n
        if out not in ("ok",):
            problems.append({"ts": r.get("ts"), "role": role, "outcome": out, "who": who, "detail": r.get("detail", "")})
    jobs = []
    for role, x in sorted(roles.items(), key=lambda kv: -(kv[1]["answered"] + kv[1]["no_answer"])):
        asked = x["answered"] + x["no_answer"]
        jobs.append({"role": role, "asked": asked, "answered": x["answered"], "no_answer": x["no_answer"],
                     "answer_rate": round(x["answered"] / asked, 3) if asked else None,
                     "asked_today": x["answered_today"] + x["no_answer_today"], "answered_today": x["answered_today"],
                     "fallbacks": x["fallbacks"], "rejected": x["rejected"],
                     "avg_ms": int(sum(x["ms"]) / len(x["ms"])) if x["ms"] else None, "p90_ms": _p90(x["ms"]),
                     "served_by": x["served_by"].most_common(4), "failures": dict(x["failures"])})
    return {"days": days, "since": f"{(now - timedelta(days=days - 1)):%Y-%m-%d}", "jobs": jobs,
            "providers": [{"model": k, **dict(v)} for k, v in sorted(providers.items(), key=lambda kv: -sum(kv[1].values()))],
            "problems": sorted(problems, key=lambda p: p["ts"] or "", reverse=True)[:25]}
