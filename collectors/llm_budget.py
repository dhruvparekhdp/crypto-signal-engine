"""
Free-tier budget for every AI provider: count requests and tokens per model, stay under the limits, and wait out a
limit for exactly as long as the provider says instead of retrying into it.

Why. On 5-6 Oct Groq answered 429 "tokens per day" about 240 times: the old breaker paused 60 seconds and tried
again, against a limit that resets hours later, and every retry was a logged error and a Telegram alert.

How.
  * LIMITS holds each model's free-tier limits (requests/min, requests/day, tokens/min, tokens/day). The bot uses
    at most SAFETY (85%) of each. settings.llm_limits (JSON) overrides any entry.
  * allow() is asked before every call. Over budget -> the call is not made (no request, no error) and the chain
    moves on to the next provider.
  * observe() is told after every call: tokens used, and Groq's x-ratelimit-* headers, which carry the provider's own
    remaining counts and reset times. Those win over our estimate.
  * cooldown_from_error() reads "Please try again in 3h12m5s" / Retry-After from a 429 and pauses that model until
    then (at least 60 s).
  * Counts are saved to data/llm_budget.json, so a restart or deploy does not reset the day's usage.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

SAFETY = 0.85
DAY = 86_400

# Free-tier limits (requests/min, requests/day, tokens/min, tokens/day). None = no limit known or none applies.
# Groq: console.groq.com/settings/limits (free plan). OpenRouter ":free" models without credits: 20 RPM, 50 RPD.
LIMITS: dict[str, dict] = {
    "groq/openai/gpt-oss-120b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200_000},
    "groq/openai/gpt-oss-20b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200_000},
    "groq/*": {"rpm": 30, "rpd": 1000, "tpm": 6000, "tpd": 200_000},
    "openrouter/*": {"rpm": 20, "rpd": 50, "tpm": None, "tpd": None},
    "gemini/*": {"rpm": 10, "rpd": 250, "tpm": 250_000, "tpd": None},
    "hf/analyst": {"rpm": 2, "rpd": None, "tpm": None, "tpd": None},          # our CPU Space: one at a time
    "hf/*": {"rpm": 5, "rpd": 100, "tpm": None, "tpd": None},
    "ollama/*": {"rpm": None, "rpd": None, "tpm": None, "tpd": None},
    "anthropic/*": {"rpm": 50, "rpd": None, "tpm": None, "tpd": None},
}


def _key(provider: str, model: str) -> str:
    return f"{provider}/{model.removesuffix('+search').removesuffix(':online')}"


def _parse_duration(text: str) -> float | None:
    """'3h12m5.5s', '41m', '7.2s', '500ms' -> seconds."""
    m = re.search(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", text or "", re.I)
    if not m:
        return None
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", m.group(1)):
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total or None


def _header_seconds(v: str | None) -> float | None:
    """Groq reset headers look like '2m59.56s' or '7.66s'; Retry-After is plain seconds."""
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return _parse_duration("try again in " + v)


class Budget:
    def __init__(self, path: str = "data/llm_budget.json", overrides: dict | None = None):
        self.path = Path(path)
        self.limits = {**LIMITS, **(overrides or {})}
        self._lock = threading.Lock()
        self.models: dict[str, dict] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────
    def _load(self) -> None:
        try:
            self.models = json.loads(self.path.read_text()).get("models", {})
        except (OSError, ValueError):
            self.models = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"models": self.models}))
            tmp.replace(self.path)
        except OSError:
            pass

    # ── bookkeeping ──────────────────────────────────────────
    def limits_for(self, key: str) -> dict:
        if key in self.limits:
            return self.limits[key]
        return self.limits.get(key.split("/", 1)[0] + "/*", {})

    def _m(self, key: str, now: float) -> dict:
        m = self.models.setdefault(key, {"calls": [], "tokens": [], "day": int(now // DAY), "day_calls": 0,
                                         "day_tokens": 0, "cooldown_until": 0.0, "last_error": "", "served": 0,
                                         "skipped": 0, "remote": {}})
        if m["day"] != int(now // DAY):                       # new UTC day: daily counts start again
            m.update(day=int(now // DAY), day_calls=0, day_tokens=0)
        m["calls"] = [t for t in m["calls"] if now - t < 60]
        m["tokens"] = [(t, n) for t, n in m["tokens"] if now - t < 60]
        return m

    def allow(self, provider: str, model: str, est_tokens: int = 1500, now: float | None = None) -> tuple[bool, float, str]:
        """(ok, seconds until it would be ok, reason). Never makes a network call."""
        now = time.time() if now is None else now
        key = _key(provider, model)
        lim = self.limits_for(key)
        with self._lock:
            m = self._m(key, now)
            if m["cooldown_until"] > now:
                return self._skip(m, False, m["cooldown_until"] - now, f"cooling down after a rate limit ({m['last_error'][:60]})")
            r = m.get("remote", {})
            if r.get("tokens_reset_at", 0) > now and r.get("tokens_left") is not None and r["tokens_left"] < est_tokens:
                return self._skip(m, False, r["tokens_reset_at"] - now, "provider says too few tokens left this minute")
            if r.get("requests_reset_at", 0) > now and r.get("requests_left") is not None and r["requests_left"] < 1:
                return self._skip(m, False, r["requests_reset_at"] - now, "provider says no requests left today")
            nxt = (int(now // DAY) + 1) * DAY
            if lim.get("rpd") and m["day_calls"] >= max(1, int(lim["rpd"] * SAFETY)):
                return self._skip(m, False, nxt - now, f"daily request budget used ({m['day_calls']}/{lim['rpd']})")
            if lim.get("tpd") and m["day_tokens"] + est_tokens > lim["tpd"] * SAFETY:
                return self._skip(m, False, nxt - now, f"daily token budget used ({m['day_tokens']:,}/{lim['tpd']:,})")
            if lim.get("rpm") and len(m["calls"]) >= max(1, int(lim["rpm"] * SAFETY)):
                return self._skip(m, False, 60 - (now - m["calls"][0]), "per-minute request budget used")
            if lim.get("tpm") and sum(n for _, n in m["tokens"]) + est_tokens > lim["tpm"] * SAFETY:
                return self._skip(m, False, 60 - (now - m["tokens"][0][0]) if m["tokens"] else 60, "per-minute token budget used")
            return True, 0.0, ""

    def _skip(self, m, ok, wait, why):
        m["skipped"] = m.get("skipped", 0) + 1
        return ok, max(0.0, wait), why

    def observe(self, provider: str, model: str, tokens: int, headers=None, now: float | None = None) -> None:
        """Record a finished call (whether or not the reply was useful) and the provider's own counters."""
        now = time.time() if now is None else now
        key = _key(provider, model)
        with self._lock:
            m = self._m(key, now)
            m["calls"].append(now)
            m["tokens"].append((now, int(tokens)))
            m["day_calls"] += 1
            m["day_tokens"] += int(tokens)
            m["served"] = m.get("served", 0) + 1
            if headers is not None:
                h = {k.lower(): v for k, v in dict(headers).items()}
                r = m.setdefault("remote", {})
                if "x-ratelimit-remaining-tokens" in h:
                    r["tokens_left"] = int(float(h["x-ratelimit-remaining-tokens"]))
                    r["tokens_reset_at"] = now + (_header_seconds(h.get("x-ratelimit-reset-tokens")) or 60)
                if "x-ratelimit-remaining-requests" in h:
                    r["requests_left"] = int(float(h["x-ratelimit-remaining-requests"]))
                    r["requests_reset_at"] = now + (_header_seconds(h.get("x-ratelimit-reset-requests")) or DAY)
            self._save()

    def cooldown_from_error(self, provider: str, model: str, error: str, retry_after: str | None = None,
                            now: float | None = None) -> float:
        """Pause a model after a rate-limit reply for as long as the provider asked (60 s minimum). Returns seconds."""
        now = time.time() if now is None else now
        key = _key(provider, model)
        wait = _header_seconds(retry_after) or _parse_duration(error)
        if wait is None:
            low = error.lower()
            wait = (int(now // DAY) + 1) * DAY - now if ("per day" in low or "tpd" in low or "rpd" in low) else 60
        wait = max(60.0, wait)
        with self._lock:
            m = self._m(key, now)
            m["cooldown_until"] = now + wait
            m["last_error"] = re.sub(r"org_[a-z0-9]+", "org_…", error)[:200]
            self._save()
        return wait

    def snapshot(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for key in sorted(self.models):
                m = self._m(key, now)
                lim = self.limits_for(key)
                out.append({"model": key, "today_calls": m["day_calls"], "today_tokens": m["day_tokens"],
                            "last_minute_calls": len(m["calls"]), "limits": lim, "safety": SAFETY,
                            "cooldown_s": max(0, round(m["cooldown_until"] - now)), "last_error": m["last_error"],
                            "skipped": m.get("skipped", 0), "served": m.get("served", 0), "remote": m.get("remote", {})})
        return out


_budget: Budget | None = None


def budget() -> Budget:
    global _budget
    if _budget is None:
        overrides = {}
        try:
            from config.settings import settings
            raw = getattr(settings, "llm_limits", "") or ""
            overrides = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 - a bad override must not stop AI calls
            overrides = {}
        import os
        _budget = Budget(os.environ.get("LLM_BUDGET_PATH", "data/llm_budget.json"), overrides=overrides)
    return _budget
