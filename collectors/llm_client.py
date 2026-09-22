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

import json
import time
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str
    endpoint: str
    key_attr: str

    @property
    def api_key(self) -> str:
        raw = getattr(settings, self.key_attr, None)
        if raw is None:
            return ""
        return raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


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
}


@dataclass
class Reply:
    """What came back, and who answered. Empty `data` means nobody did."""

    data: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    attempts: list[str] = field(default_factory=list)

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
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
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
    if provider.name != "gemini":
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {provider.api_key}",
               "Content-Type": "application/json"}
    if provider.name == "openrouter":
        # OpenRouter asks callers to identify themselves; without these the
        # request works but is rate-limited more aggressively.
        headers["HTTP-Referer"] = "https://github.com/dhruvparekhdp/crypto-signal-engine"
        headers["X-Title"] = "crypto-signal-engine"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(provider.endpoint, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"{provider.name} returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()["choices"][0]["message"]["content"]


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

    for provider_name, model in chain_for(role):
        provider = PROVIDERS[provider_name]
        attempts.append(f"{provider_name}/{model}")
        try:
            if provider.kind == ANTHROPIC_SHAPED:
                text = await _call_anthropic(model, system, user, max_tokens, timeout)
            else:
                text = await _call_openai_shaped(
                    provider, model, system, user, max_tokens, temperature, timeout)

            data = _extract_json(text)
            if not data:
                log.warning("llm_reply_was_not_json", role=role,
                            provider=provider_name, model=model, head=text[:120])
                continue

            elapsed = int((time.perf_counter() - started) * 1000)
            if len(attempts) > 1:
                log.info("llm_served_by_fallback", role=role,
                         served_by=f"{provider_name}/{model}", after=attempts[:-1])
            return Reply(data=data, provider=provider_name, model=model,
                         latency_ms=elapsed, attempts=attempts)

        except Exception as exc:
            log.warning("llm_provider_failed", role=role, provider=provider_name,
                        model=model, error=str(exc)[:200])

    log.warning("llm_no_provider_answered", role=role, attempts=attempts)
    return Reply(latency_ms=int((time.perf_counter() - started) * 1000),
                 attempts=attempts)
