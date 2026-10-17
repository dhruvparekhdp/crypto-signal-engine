"""AI provider bake-off (owner, 15 Oct 2026): every configured model on the same real work, measured.

    PYTHONPATH=. python scripts/ai_bakeoff.py SAMPLE.json > result.json      # on the server (keys live there)

Tasks
  news   120 stored headlines in batches of 30: relevant? score -1..+1, confidence, event type (JSON)
  trap   a release that has NOT happened yet (US PPI, 15 Oct 12:30 UTC): the honest answer is "not released"
  price  current BTC price: tells search-capable models from ones that guess
Each call's latency, failure and JSON validity are recorded. Scoring against what BTC did next happens offline
(scripts/ai_bakeoff_report.py). Free-tier limits are respected: Groq calls are spaced for 8k tokens/minute,
OpenRouter's free key gets ~4 calls per model.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

EVENT_TYPES = ("rate_decision, inflation_data, jobs_data, war, tariff, regulation, etf_flow, liquidation, hack, "
               "adoption, exchange_outage, macro_other, crypto_other, noise")
NEWS_SYSTEM = (
    "You score news for a crypto trading desk. For each numbered headline decide: relevant (could it move BTC or "
    "the major coins within 24 hours?), score from -1 (bearish for crypto) to +1 (bullish), confidence 0..1, and "
    f"event_type from: {EVENT_TYPES}. Judge only from the headline; routine or off-topic items are noise with "
    "score 0. Reply with JSON only: {\"items\": [{\"i\": 1, \"relevant\": true, \"score\": -0.4, "
    "\"confidence\": 0.7, \"event_type\": \"etf_flow\"}, ...]} with one entry for every headline.")
TRAP_SYSTEM = ("You answer factual questions for a trading desk. If something has not happened yet or you cannot "
               "verify it, say so - never guess. Reply JSON only.")
TRAP_USER = ("Current time: {now} UTC. The US Producer Price Index (PPI) for September 2026 is scheduled for "
             "15 Oct 2026 at 12:30 UTC. Has it been released, and what was the month-on-month figure? Reply "
             "{{\"released\": true or false, \"value\": \"figure or null\", \"how_known\": \"search|memory|unknown\"}}")
PRICE_USER = ("Current time: {now} UTC. What is the BTC/USD price right now? Reply {{\"price\": number or null, "
              "\"how_known\": \"search|memory|unknown\"}}")

CANDIDATES = [
    # provider, model, does it search?, seconds to wait after each call (free-tier pacing)
    ("groq", "openai/gpt-oss-120b", False, 40), ("groq", "openai/gpt-oss-20b", False, 40),
    ("groq", "qwen/qwen3.8-27b", False, 40, 10),
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", False, 4),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", False, 4),
    ("openrouter", "google/gemma-4-31b-it:free", False, 4),
    ("openrouter", "thinkingmachines/inkling:free", False, 4),
    ("openrouter", "poolside/laguna-s-2.1:free", False, 4),
    ("hf", "meta-llama/Llama-3.1-8B-Instruct", False, 3),
    ("gemini", "gemini-3.1-flash", False, 5), ("gemini", "gemini-3.1-pro-preview", False, 10),
    ("anthropic", "claude-haiku-5-5", False, 2), ("anthropic", "claude-sonnet-5-5", False, 2),
]
SEARCH_CANDIDATES = [("groq", "openai/gpt-oss-120b+search", 40), ("groq", "openai/gpt-oss-20b+search", 40),
                     ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free+search", 4),
                     ("hf", "meta-llama/Llama-3.1-8B-Instruct+search", 3)]


async def _call(provider_name: str, model: str, system: str, user: str, max_tokens: int, timeout: float):
    from collectors.llm_client import PROVIDERS, _call_anthropic, _call_hf, _call_openai_shaped, _extract_json
    p = PROVIDERS[provider_name]
    t = time.perf_counter()
    try:
        if p.kind == "anthropic":
            text = await _call_anthropic(model, system, user, max_tokens, timeout)
        elif provider_name == "hf":
            text = await _call_hf(model, system, user, max_tokens, 0.0, timeout)
        else:
            text = await _call_openai_shaped(p, model, system, user, max_tokens, 0.0, timeout)
        data = _extract_json(text)
        repaired = False
        if not data and text.lstrip().startswith('{"{'):
            data, repaired = _extract_json(text.lstrip()[2:]), True
        return {"ok": bool(data), "repaired": repaired, "ms": int((time.perf_counter() - t) * 1000), "data": data,
                "tokens_out_est": len(text) // 4, "error": "" if data else "no JSON: " + text[:120]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": int((time.perf_counter() - t) * 1000), "data": {}, "error": str(e)[:200]}


async def finbert(sample: list[dict]) -> dict:
    import httpx

    from config.settings import settings
    base = (settings.hf_base_url or "").rstrip("/")
    tok = settings.hf_api_token.get_secret_value() if settings.hf_api_token else ""
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(base + "/v1/classify", headers={"Authorization": f"Bearer {tok}"},
                             json={"texts": [s["headline"] for s in sample]})
        res = r.json().get("results") or []
        items = [{"i": s["i"], "score": round(x.get("positive", 0) - x.get("negative", 0), 3),
                  "relevant": x.get("neutral", 1) < 0.6, "confidence": round(1 - x.get("neutral", 0), 3)}
                 for s, x in zip(sample, res)]
        return {"provider": "hf", "model": "space/finbert", "calls": [{"ok": bool(items), "ms": int((time.perf_counter() - t) * 1000)}],
                "items": items}
    except Exception as e:  # noqa: BLE001
        return {"provider": "hf", "model": "space/finbert", "calls": [{"ok": False, "error": str(e)[:200]}], "items": []}


async def main() -> None:
    from scripts._ai_guard import require_quota_window
    require_quota_window()
    from config.overrides import apply
    from config.settings import settings
    from scheduler.keys_page import _stored
    apply(settings, await _stored())
    from collectors.llm_client import PROVIDERS
    sample = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else json.load(sys.stdin)
    only = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None      # model names to run
    now = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    out = {"started": now, "results": [await finbert(sample)], "traps": []}
    print(json.dumps({"result": out["results"][0], "traps": []}), flush=True)
    print("finbert done", file=sys.stderr, flush=True)
    for cand in CANDIDATES:
        provider, model, _search, pace = cand[:4]
        size = cand[4] if len(cand) > 4 else 30
        if only and model not in only:
            continue
        if not PROVIDERS[provider].configured:
            out["results"].append({"provider": provider, "model": model, "skipped": "no key saved"})
            print(json.dumps({"result": out["results"][-1], "traps": []}), flush=True)
            continue
        res = {"provider": provider, "model": model, "calls": [], "items": []}
        for b in range(0, len(sample), size):
            batch = sample[b:b + size]
            user = "\n".join(f"{s['i']}. [{s['source']}, {s['published_at']} UTC] {s['headline']}" for s in batch)
            r = await _call(provider, model, NEWS_SYSTEM, user, 80 * size, 120)
            res["calls"].append({k: v for k, v in r.items() if k != "data"})
            got = (r["data"] or {}).get("items") or []
            res["items"] += [x for x in got if isinstance(x, dict)]
            print(provider, model, "batch", b // size + 1, "ok" if r["ok"] else r["error"][:80], r["ms"], "ms",
                  file=sys.stderr, flush=True)
            await asyncio.sleep(pace)
            if not r["ok"] and b == 0 and "429" in r["error"]:
                break                                      # rate-limited from the start: do not burn the quota
        for kind, user in (("trap", TRAP_USER), ("price", PRICE_USER)):
            r = await _call(provider, model, TRAP_SYSTEM, user.format(now=time.strftime("%Y-%m-%d %H:%M", time.gmtime())), 300, 60)
            out["traps"].append({"provider": provider, "model": model, "kind": kind, **r})
            await asyncio.sleep(pace)
        out["results"].append(res)
        print(json.dumps({"result": res, "traps": [t for t in out["traps"] if t["model"] == model]}), flush=True)
    for provider, model, pace in SEARCH_CANDIDATES:
        if not PROVIDERS[provider].configured or (only and model not in only):
            continue
        for kind, user in (("trap", TRAP_USER), ("price", PRICE_USER)):
            r = await _call(provider, model, TRAP_SYSTEM, user.format(now=time.strftime("%Y-%m-%d %H:%M", time.gmtime())), 600, 90)
            out["traps"].append({"provider": provider, "model": model, "kind": kind, **r})
            print(json.dumps({"traps": [out["traps"][-1]]}), flush=True)
            print("search", provider, model, kind, "ok" if r["ok"] else r["error"][:80], file=sys.stderr, flush=True)
            await asyncio.sleep(pace)
    print(json.dumps({"done": True}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
