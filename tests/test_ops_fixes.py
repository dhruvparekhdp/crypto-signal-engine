"""6 Oct production fixes: AI fallback with search, fewer AI calls, fewer outage alerts, trend check off."""
import time
from unittest.mock import AsyncMock, patch

import pytest

from analysis import regime_gate as rg


def test_trend_check_is_off_by_default_and_volatility_check_stays():
    from config.settings import Settings
    assert Settings.model_fields["swing_regime_adx_max"].default == 0.0
    import numpy as np
    c = 100 * np.exp(np.linspace(0, 1.5, 300))                      # a hard one-way trend: ADX far above 30
    assert not rg.judge(None, c * 1.005, c * 0.995, c, adx_max=0.0).would_skip
    assert rg.judge(None, c * 1.005, c * 0.995, c, adx_max=30.0).reasons == ["strong_trend"]


def test_search_roles_have_a_non_groq_fallback_and_run_less_often():
    from config.settings import Settings
    for role in ("llm_chain_briefing", "llm_chain_attribution", "llm_chain_briefing_calm"):
        chain = Settings.model_fields[role].default
        assert "openrouter:" in chain and chain.strip().endswith("+search")
    assert Settings.model_fields["market_briefing_minutes"].default == 60
    assert Settings.model_fields["move_attribution_minutes"].default == 180


@pytest.mark.asyncio
async def test_openrouter_search_injects_web_results_instead_of_groq_tools():
    from collectors import llm_client as lc
    sent = {}

    class Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
        text = ""

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None, **k):
            sent["payload"] = json
            return Resp()

    provider = lc.PROVIDERS["openrouter"]
    with patch.object(lc, "_search_ddg", lambda q, n: "- BTC rallies on ETF inflows"), \
         patch.object(lc.httpx, "AsyncClient", Client):
        await lc._call_openai_shaped(provider, "qwen/qwen3.8-27b:free+search", "sys", "why did BTC move", 200, 0.2, 10)
    p = sent["payload"]
    assert p["model"] == "qwen/qwen3.8-27b:free" and "tools" not in p
    assert "BTC rallies on ETF inflows" in p["messages"][0]["content"]
    assert p["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_outage_alerts_at_most_once_per_six_hours_per_role():
    from scheduler.runner import AppRunner
    r = AppRunner()
    r.notifier = AsyncMock()
    for _ in range(10):
        await r._alert_llm_outage("attribution", ["groq 429"])
    assert r.notifier.send_text.await_count == 1
    r._llm_alert_at["attribution"] = time.monotonic() - 6 * 3600 - 1
    await r._alert_llm_outage("attribution", ["groq 429"])
    assert r.notifier.send_text.await_count == 2


@pytest.mark.asyncio
async def test_analyst_without_a_space_fails_fast_instead_of_paid_inference():
    from collectors import llm_client as lc
    with patch.object(lc.settings, "hf_base_url", ""), patch.object(lc, "_search_ddg", lambda q, n: ""):
        with pytest.raises(RuntimeError, match="no Space configured"):
            await lc._call_hf("analyst+search", "sys", "user", 100, 0.2, 10)


@pytest.mark.asyncio
async def test_our_space_gets_its_own_key_json_mode_and_a_long_timeout():
    from pydantic import SecretStr

    from collectors import llm_client as lc
    seen = {}

    class Resp:
        status_code = 200
        text = ""
        headers = {}
        def json(self):
            return {"choices": [{"message": {"content": "{\"why\": \"ETF inflows\"}"}}]}

    class Client:
        def __init__(self, timeout=None, **k):
            seen["timeout"] = timeout
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            seen.update(url=url, payload=json, headers=headers)
            return Resp()

    with patch.object(lc.settings, "hf_base_url", "https://me-crypto-analyst.hf.space"), \
         patch.object(lc.settings, "hf_space_api_key", SecretStr("space-key")), \
         patch.object(lc, "_search_ddg", lambda q, n: "- SOL jumps 3%"), patch.object(lc.httpx, "AsyncClient", Client):
        out = await lc._call_hf("analyst+search", "sys", "why did SOL move", 300, 0.2, 20)
    assert out == "{\"why\": \"ETF inflows\"}"
    assert seen["url"] == "https://me-crypto-analyst.hf.space/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer space-key"
    assert seen["payload"]["model"] == "analyst" and seen["payload"]["response_format"] == {"type": "json_object"}
    assert "SOL jumps 3%" in seen["payload"]["messages"][0]["content"]
    assert seen["timeout"] >= 240
