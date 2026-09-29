"""
One way to ask a model something, across four providers.

Why a layer at all
------------------
Groq was hardcoded into the sentinel: its endpoint, its auth header, its
model-not-found retry. That was fine while Groq was the only account. It
stops being fine for two reasons, and neither is about wanting variety.

The first is that the pre-trade review sits in the signal path. When Groq
rate-limits — and a free tier does, usually at the worst moment — the review
returns nothing, the signal fires unreviewed, and the log line for that is a
debug-level exception nobody reads. A second provider behind the first turns
an outage into a slower answer instead of a silently missing one.

The second is that these calls are not one job. A pre-trade check has to come
back before the price moves, so it wants the fastest model that can follow
instructions. A post-mortem on a closed trade has no deadline at all and is
building a dataset worth keeping, so it wants the best reasoning available. A
weekly pass over aggregated statistics wants the strongest model there is and
runs four times a month. Pointing all three at one model gets at most one of
them right.

Roles, not providers
--------------------
Callers ask for a ROLE — "pre_trade", "post_trade", "research" — and the
chain behind that role is configuration. Nothing in the trading code names a
vendor, so changing where post-mortems run is an env var rather than a patch.

Provenance is recorded
----------------------
Every reply carries which provider and model actually served it. Post-mortems
accumulate into a labelled dataset, and a dataset whose rows were written by
three different models with no way to tell which is a dataset you cannot
later trust. `served_by` is not diagnostics; it is a column.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog

from config.settings import settings

log = structlog.get_logger()

# Groq, OpenRouter and Google AI Studio all expose an OpenAI-shaped
# /chat/completions, so one adapter covers three of the four. Anthropic has
# its own request shape and its own SDK, which is the fourth.
OPENAI_SHAPED = "openai"
ANTHROPIC_SHAPED = "anthropic"


def should_call_again(elapsed_ratio: float, min_elapsed_ratio: float,
                      current_value: float, last_value: float,
                      min_delta: float) -> bool:
    """
    Is it worth spending another AI call on this?

    Extracted from mirror-review's re-review gate (analysis/mirror_review.py
    / scheduler/runner.py::_mirror_review_job), which fires a second AI call
    on a tracked candidate only once enough of its own budgeted window has
    passed AND a cheap local re-score has moved enough to be worth a second
    opinion. Both conditions are cheap and local — no AI, no I/O — which is
    what makes them worth checking before ever reaching for a call.

    `elapsed_ratio` / `min_elapsed_ratio` is deliberately a ratio rather than
    a fixed "seconds since last call": mirror-review's own window is a
    fraction of the SIGNAL's timeframe (20 minutes of a 1h signal, say), not
    a role-wide cadence, and different roles measure "enough time" in
    different units. A caller that wants a plain wall-clock gap passes
    elapsed_seconds / min_gap_seconds for both ratio arguments instead — the
    function only ever compares a ratio to 1.0, so either usage is exact.

    Pure and side-effect free, same as its callers demand: this only answers
    "should", never itself places the call.
    """
    moved = abs(current_value - last_value) >= min_delta
    return elapsed_ratio >= min_elapsed_ratio and moved


# Per-role daily AI call counter. Rolls over at UTC midnight rather than a
# rolling 24h window — the same simplification event_monitor's own daily cap
# (settings.event_monitor_daily_cap, scheduler/runner.py's _monitor_calls)
# already makes, so this follows an existing pattern instead of inventing a
# second one. In-process only: a redeploy resets it, which is fine for a
# "is something runaway right now" dashboard number, not a billing ledger.
_calls_today: dict[str, int] = {}
_calls_today_date: date | None = None


def _record_call(role: str) -> None:
    global _calls_today_date
    today = datetime.now(UTC).date()
    if _calls_today_date != today:
        _calls_today.clear()
        _calls_today_date = today
    _calls_today[role] = _calls_today.get(role, 0) + 1


def calls_today() -> dict[str, int]:
    """{role: count} of ask_json() calls made since the last UTC midnight.

    Every AI call in this codebase funnels through ask_json(), so this covers
    every role automatically — surfaced on /api/debug/perf so a runaway
    feature (a role suddenly calling far more than the others) is visible
    without reading logs.
    """
    today = datetime.now(UTC).date()
    if _calls_today_date != today:
        return {}
    return dict(_calls_today)


_circuit_breakers: dict[str, datetime] = {}  # provider_name -> active_until_utc


def trip_circuit_breaker(provider_name: str, seconds: int = 45, reason: str = "") -> None:
    """Pause a provider for 45 seconds when hitting rate limits or repeated errors."""
    until = datetime.now(UTC) + timedelta(seconds=seconds)
    _circuit_breakers[provider_name] = until
    log.warning("llm_circuit_breaker_tripped", provider=provider_name, pause_seconds=seconds,
                until=until.isoformat(), reason=reason[:120])


def is_circuit_open(provider_name: str) -> bool:
    """Return True if this provider is currently paused in a circuit breaker cooldown."""
    until = _circuit_breakers.get(provider_name)
    if not until:
        return False
    if datetime.now(UTC) >= until:
        del _circuit_breakers[provider_name]
        log.info("llm_circuit_breaker_reset", provider=provider_name)
        return False
    return True


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    endpoint: str
    key_attr: str
    # A provider whose address is configuration rather than a constant: a
    # self-hosted model lives wherever its owner put it, and that address is
    # also the switch. Empty means "not set up", so the chain skips it.
    endpoint_attr: str = ""
    # A model running on your own machine has nothing to authenticate to.
    needs_key: bool = True

    @property
    def api_key(self) -> str:
        raw = getattr(settings, self.key_attr, None)
        if raw is None:
            return ""
        return raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)

    @property
    def url(self) -> str:
        if not self.endpoint_attr:
            return self.endpoint
        base = (getattr(settings, self.endpoint_attr, "") or "").strip().rstrip("/")
        return f"{base}{self.endpoint}" if base else ""

    @property
    def configured(self) -> bool:
        if self.name == "hf":
            return bool(self.api_key) or bool(self.url)
        return bool(self.api_key) if self.needs_key else bool(self.url)


PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        "groq", OPENAI_SHAPED,
        "https://api.groq.com/openai/v1/chat/completions", "groq_api_key"),
    "openrouter": Provider(
        "openrouter", OPENAI_SHAPED,
        "https://openrouter.ai/api/v1/chat/completions", "openrouter_api_key"),
    "gemini": Provider(
        "gemini", OPENAI_SHAPED,
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini_api_key"),
    "anthropic": Provider(
        "anthropic", ANTHROPIC_SHAPED,
        "https://api.anthropic.com/v1/messages", "anthropic_api_key"),
    # A model on hardware you own. Ollama serves an OpenAI-shaped
    # /v1/chat/completions, so it needs no adapter of its own — only an
    # address and permission to have no key.
    "ollama": Provider(
        "ollama", OPENAI_SHAPED, "/v1/chat/completions", "",
        endpoint_attr="ollama_base_url", needs_key=False),
    # Hugging Face: serverless inference API or custom space endpoint.
    # Supported natively via huggingface_hub AsyncInferenceClient with DDGS web search.
    "hf": Provider(
        "hf", OPENAI_SHAPED, "/v1/chat/completions", "hf_api_token",
        endpoint_attr="hf_base_url", needs_key=True),
}


@dataclass
class Reply:
    """What came back, and who answered. Empty `data` means nobody did."""

    data: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    attempts: list[str] = field(default_factory=list)
    # "provider/model: why" for each link that did not answer, so a page can
    # say why the web-search model was skipped without anyone reading logs.
    failures: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.data)

    @property
    def served_by(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else ""


def _parse_chain(raw: str) -> list[tuple[str, str]]:
    """
    "groq:llama-3.3-70b, openrouter:qwen/qwen3-32b" -> [(provider, model), ...]

    A string rather than structured config because it lives in an env var and
    the whole point is changing it without a deploy. Entries naming an unknown
    provider are dropped with a warning rather than raising: a typo in one
    fallback should not take down the primary.
    """
    chain: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        provider, _, model = part.partition(":")
        provider, model = provider.strip().lower(), model.strip()
        if provider not in PROVIDERS:
            log.warning("llm_unknown_provider", provider=provider, entry=part)
            continue
        if not model:
            log.warning("llm_entry_has_no_model", entry=part)
            continue
        chain.append((provider, model))
    return chain


def chain_for(role: str) -> list[tuple[str, str]]:
    """The configured chain for a role, minus providers with no key set."""
    raw = getattr(settings, f"llm_chain_{role}", "") or ""
    full = _parse_chain(raw)
    usable = [(p, m) for p, m in full if PROVIDERS[p].configured]
    if full and not usable:
        log.warning("llm_no_configured_provider_for_role", role=role,
                    wanted=[p for p, _ in full])
    return usable


def _extract_json(text: str) -> dict[str, Any]:
    """
    Pull an object out of a reply.

    Models asked for JSON mostly return JSON, and sometimes return JSON inside
    a ```json fence or after a sentence of preamble. Not every provider
    supports a response_format that forbids that, so the slicing fallback is
    load-bearing rather than defensive.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
        # Callers index the reply as a dict; a bare list or number from a
        # confused model must read as "no answer", not raise downstream.
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(text[start:end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}


async def _call_openai_shaped(provider: Provider, model: str, system: str,
                              user: str, max_tokens: int, temperature: float,
                              timeout: float) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # Gemini's compatibility endpoint rejects response_format; Groq and
    # OpenRouter accept it and it measurably reduces prose around the object.
    # "openai/gpt-oss-120b+search" turns on Groq's built-in browser_search
    # tool, the replacement for the decommissioned groq/compound. Tools and
    # json_object do not mix, so a searching call relies on _extract_json.
    search = model.endswith("+search")
    if search:
        model = model[:-len("+search")]
        payload["model"] = model
        payload["tools"] = [{"type": "browser_search"}]
    if provider.name != "gemini" and not search:
        payload["response_format"] = {"type": "json_object"}
    # Reasoning models spend max_tokens on thinking before they write the
    # answer, so a 120-token budget comes back empty and the chain falls
    # through silently. qwen3 on Ollama thinks unless told not to ("none"
    # maps to think:false on /v1); gpt-oss on Groq is kept to "low".
    if provider.name == "ollama":
        payload["reasoning_effort"] = "none"
    elif provider.name == "groq" and model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"

    headers = {"Content-Type": "application/json"}
    if provider.needs_key or provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    if provider.name == "openrouter":
        # OpenRouter asks callers to identify themselves; without these the
        # request works but is rate-limited more aggressively.
        headers["HTTP-Referer"] = "https://github.com/dhruvparekhdp/crypto-signal-engine"
        headers["X-Title"] = "crypto-signal-engine"

    if search:
        system = (system + "\nNote: Do not call custom functions or tools named 'json'. "
                           "Only use browser_search if needed for news research, and write your "
                           "final output as plain text containing a valid JSON object.")
        payload["messages"] = [{"role": "system", "content": system},
                                {"role": "user", "content": user}]

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(provider.url, json=payload, headers=headers)
        if resp.status_code == 400 and search and "tool" in resp.text.lower():
            # Groq search tool error fallback: retry without tool attachment
            log.warning("groq_search_tool_error_fallback", error=resp.text[:120])
            payload.pop("tools", None)
            payload["response_format"] = {"type": "json_object"}
            resp = await client.post(provider.url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"{provider.name} returned {resp.status_code}: {resp.text[:200]}")
        choices = resp.json().get("choices", [])
        if not choices:
            return ""
        return choices[0]["message"].get("content", "") or ""


async def _call_anthropic(model: str, system: str, user: str,
                          max_tokens: int, timeout: float) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=PROVIDERS["anthropic"].api_key, timeout=timeout)
    message = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in message.content if b.type == "text")


def _search_ddg(query: str, max_results: int = 5) -> str:
    """Run a real-time web search on EC2 via DuckDuckGo."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return ""
        snippets = []
        for r in results:
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            if title and body:
                snippets.append(f"- {title}: {body}")
        return "\n".join(snippets)
    except Exception as exc:
        log.warning("hf_search_ddg_failed", query=query[:60], error=str(exc))
        return ""


def _derive_search_query(user: str) -> str:
    """Extract a clean, targeted query for DuckDuckGo from the prompt text."""
    lowered = user.lower()
    if "briefing" in lowered or "brief me" in lowered:
        return "crypto market news bitcoin ethereum macro economy today"
    if "attribution" in lowered or "why" in lowered or "%" in user:
        return "crypto market movers bitcoin altcoins news why market moving today"
    words = [w for w in user.split() if "{" not in w and "}" not in w and len(w) < 20]
    clean = " ".join(words[:15])
    return f"crypto news {clean}" if clean else "crypto market news today"


async def _call_hf(model: str, system: str, user: str, max_tokens: int,
                   temperature: float, timeout: float) -> str:
    """
    Call Hugging Face via serverless router or custom space endpoint.
    If the model ends in +search or :online, performs real-time DuckDuckGo
    search on EC2 and injects fresh snippets into the prompt.
    """
    search = model.endswith("+search") or model.endswith(":online")
    if model.endswith("+search"):
        model = model[:-len("+search")]
    elif model.endswith(":online"):
        model = model[:-len(":online")]

    if model in ("analyst", "default", ""):
        model = "meta-llama/Llama-3.3-70B-Instruct"

    if search:
        query = _derive_search_query(user)
        snippets = await asyncio.to_thread(_search_ddg, query, 5)
        if snippets:
            system = (
                f"{system}\n\n"
                f"Real-time web search results (retrieved just now for context):\n"
                f"{snippets}\n\n"
                "Use the factual findings above from recent news to ground your response. "
                "Output valid JSON only."
            )

    token = PROVIDERS["hf"].api_key
    base_url = (getattr(settings, "hf_base_url", "") or "").strip().rstrip("/") or None

    try:
        from huggingface_hub import AsyncInferenceClient

        client = AsyncInferenceClient(
            token=token or None,
            base_url=base_url,
            timeout=timeout,
        )
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if resp.choices:
            return resp.choices[0].message.content or ""
        return ""
    except ImportError:
        endpoint = base_url or "https://router.huggingface.co"
        url = f"{endpoint}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"hf returned {resp.status_code}: {resp.text[:200]}")
            choices = resp.json().get("choices", [])
            return choices[0]["message"].get("content", "") if choices else ""


async def ask_json(role: str, system: str, user: str, *, max_tokens: int = 512,
                   temperature: float = 0.2, timeout: float = 20.0) -> Reply:
    """
    Ask the chain for this role until one answers with a JSON object.

    Never raises. Every caller is on a path where the trade has already been
    decided or already closed, and an unavailable reviewer must cost the
    review and nothing else. A caller that cannot tell "the model said
    nothing" from "the model was unreachable" gets the same empty Reply for
    both, which is correct: neither is an opinion.

    A provider that answers with something that is not an object counts as a
    failure and the chain moves on — an empty dict from a reachable provider
    is indistinguishable downstream from an outage, so it should not end the
    search.
    """
    started = time.perf_counter()
    attempts: list[str] = []
    failures: list[str] = []
    _record_call(role)

    for provider_name, model in chain_for(role):
        attempts.append(f"{provider_name}/{model}")
        if is_circuit_open(provider_name):
            failures.append(f"{provider_name}/{model}: circuit breaker open (paused 45s)")
            continue

        if len(attempts) > 1:
            # Pacing gap: at least 1.0 second delay between 1st and 2nd API attempts
            await asyncio.sleep(1.0)

        provider = PROVIDERS[provider_name]
        try:
            if provider.kind == ANTHROPIC_SHAPED:
                text = await _call_anthropic(model, system, user, max_tokens, timeout)
            elif provider_name == "hf":
                text = await _call_hf(model, system, user, max_tokens, temperature, timeout)
            else:
                text = await _call_openai_shaped(
                    provider, model, system, user, max_tokens, temperature, timeout)

            data = _extract_json(text)
            if not data:
                log.warning("llm_reply_was_not_json", role=role,
                            provider=provider_name, model=model, head=text[:120])
                failures.append(f"{provider_name}/{model}: reply was not JSON "
                                f"({text.strip()[:80]!r})")
                continue

            elapsed = int((time.perf_counter() - started) * 1000)
            if len(attempts) > 1:
                log.info("llm_served_by_fallback", role=role,
                         served_by=f"{provider_name}/{model}", after=attempts[:-1])
            return Reply(data=data, provider=provider_name, model=model,
                         latency_ms=elapsed, attempts=attempts, failures=failures)

        except Exception as exc:
            err_str = str(exc).lower()
            if any(k in err_str for k in ("429", "rate limit", "quota", "too many requests", "resource_exhausted")):
                trip_circuit_breaker(provider_name, seconds=45, reason=str(exc))
            log.warning("llm_provider_failed", role=role, provider=provider_name,
                        model=model, error=str(exc)[:200])
            failures.append(f"{provider_name}/{model}: {str(exc)[:160] or type(exc).__name__}")

    log.warning("llm_no_provider_answered", role=role, attempts=attempts)
    return Reply(latency_ms=int((time.perf_counter() - started) * 1000),
                 attempts=attempts, failures=failures)
