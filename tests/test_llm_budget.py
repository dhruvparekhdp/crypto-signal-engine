"""AI call budget: stay under free-tier limits, wait out a rate limit for as long as the provider says, survive restarts."""
import pytest

from collectors.llm_budget import DAY, Budget, _parse_duration

T0 = 1_791_000_000.0


def b(tmp_path, **ov):
    return Budget(str(tmp_path / "budget.json"), overrides=ov)


def test_groq_wait_messages_are_parsed():
    assert _parse_duration("Rate limit reached ... Please try again in 3h12m5.5s. Need more tokens?") == pytest.approx(11525.5)
    assert _parse_duration("Please try again in 7.66s") == pytest.approx(7.66)
    assert _parse_duration("Please try again in 500ms") == pytest.approx(0.5)
    assert _parse_duration("no hint here") is None


def test_a_429_cools_the_model_down_for_the_time_the_provider_gives(tmp_path):
    x = b(tmp_path)
    wait = x.cooldown_from_error("groq", "openai/gpt-oss-120b+search",
                                 "429 tokens per day (TPD): Limit 200000. Please try again in 2h0m0s", now=T0)
    assert wait == pytest.approx(7200)
    ok, w, why = x.allow("groq", "openai/gpt-oss-120b", now=T0 + 60)
    assert not ok and w == pytest.approx(7140) and "cooling down" in why
    assert x.allow("groq", "openai/gpt-oss-120b", now=T0 + 7201)[0]


def test_a_daily_limit_without_a_time_waits_until_utc_midnight(tmp_path):
    x = b(tmp_path)
    wait = x.cooldown_from_error("groq", "openai/gpt-oss-20b", "429 Rate limit on tokens per day (TPD)", now=T0)
    assert wait == pytest.approx((int(T0 // DAY) + 1) * DAY - T0)


def test_per_minute_and_daily_budgets_are_respected_with_a_safety_margin(tmp_path):
    x = b(tmp_path, **{"openrouter/*": {"rpm": 10, "rpd": 20, "tpm": None, "tpd": None}})
    for i in range(8):                                   # 85% of 10 per minute
        assert x.allow("openrouter", "m:free", now=T0 + i)[0]
        x.observe("openrouter", "m:free", 100, now=T0 + i)
    ok, wait, why = x.allow("openrouter", "m:free", now=T0 + 9)
    assert not ok and "per-minute" in why and 0 < wait <= 60
    for i in range(10):
        x.observe("openrouter", "m:free", 100, now=T0 + 120 + i * 61)
    assert "daily request budget" in x.allow("openrouter", "m:free", now=T0 + 2000)[2]
    assert x.allow("openrouter", "m:free", now=(int(T0 // DAY) + 1) * DAY + 5)[0]   # new UTC day


def test_daily_token_budget(tmp_path):
    x = b(tmp_path)
    x.observe("groq", "openai/gpt-oss-120b", 169_000, now=T0)
    assert not x.allow("groq", "openai/gpt-oss-120b", est_tokens=2000, now=T0 + 120)[0]


def test_providers_own_counters_win(tmp_path):
    x = b(tmp_path)
    x.observe("groq", "openai/gpt-oss-120b", 500, headers={"x-ratelimit-remaining-tokens": "300",
                                                          "x-ratelimit-reset-tokens": "20s"}, now=T0)
    ok, wait, why = x.allow("groq", "openai/gpt-oss-120b", est_tokens=1500, now=T0 + 1)
    assert not ok and "too few tokens" in why and wait == pytest.approx(19)


def test_counts_survive_a_restart(tmp_path):
    x = b(tmp_path)
    x.observe("groq", "openai/gpt-oss-120b", 1234, now=T0)
    x.cooldown_from_error("groq", "openai/gpt-oss-120b", "try again in 10m", now=T0)
    y = b(tmp_path)
    snap = {m["model"]: m for m in y.snapshot(now=T0 + 1)}["groq/openai/gpt-oss-120b"]
    assert snap["today_tokens"] == 1234 and snap["cooldown_s"] == 599


@pytest.mark.asyncio
async def test_the_chain_skips_a_model_out_of_budget_without_calling_it(tmp_path, monkeypatch):
    from collectors import llm_budget, llm_client
    x = b(tmp_path)
    x.cooldown_from_error("groq", "openai/gpt-oss-120b", "try again in 1h", now=None)
    monkeypatch.setattr(llm_budget, "_budget", x)
    called = []

    async def fake(provider, model, *a, **k):
        called.append(model)
        return '{"ok": true}'

    monkeypatch.setattr(llm_client, "_call_openai_shaped", fake)
    monkeypatch.setattr(llm_client, "chain_for", lambda role: [("groq", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b")])
    r = await llm_client.ask_json("briefing", "sys", "user")
    assert called == ["openai/gpt-oss-20b"] and r.data == {"ok": True}
    assert any("skipped" in f for f in r.failures)


def test_one_search_call_bigger_than_the_minute_allowance_still_goes_when_the_minute_is_empty(tmp_path):
    """The first version skipped every Groq search call: its estimate alone exceeded 85% of 8,000 tokens/min."""
    x = b(tmp_path)
    assert x.allow("groq", "openai/gpt-oss-120b+search", est_tokens=9000, now=T0)[0]
    x.observe("groq", "openai/gpt-oss-120b", 9000, now=T0)
    assert not x.allow("groq", "openai/gpt-oss-120b+search", est_tokens=9000, now=T0 + 10)[0]
    assert x.allow("groq", "openai/gpt-oss-120b+search", est_tokens=9000, now=T0 + 61)[0]


def test_a_model_that_stopped_being_free_is_benched(tmp_path):
    x = b(tmp_path)
    x.bench("openrouter", "qwen/qwen3.8-27b:free+search", 6 * 3600, "404 This model is unavailable for free", now=T0)
    ok, wait, why = x.allow("openrouter", "qwen/qwen3.8-27b:free", now=T0 + 60)
    assert not ok and wait == pytest.approx(6 * 3600 - 60)


def test_outage_alerts_are_remembered_across_restarts(tmp_path):
    x = b(tmp_path)
    assert x.alert_due("briefing", now=T0) and not x.alert_due("briefing", now=T0 + 60)
    assert not b(tmp_path).alert_due("briefing", now=T0 + 3600)
    assert b(tmp_path).alert_due("briefing", now=T0 + 6 * 3600 + 1)
    assert all(not m["model"].startswith("_") for m in x.snapshot(now=T0))


@pytest.mark.asyncio
async def test_an_unconfigured_space_is_skipped_without_a_call_or_a_logged_failure(monkeypatch):
    from collectors import llm_client
    monkeypatch.setattr(llm_client.settings, "hf_base_url", "")
    called = []

    async def fake_hf(*a, **k):
        called.append(a)
        return "{}"

    monkeypatch.setattr(llm_client, "_call_hf", fake_hf)
    monkeypatch.setattr(llm_client, "chain_for", lambda role: [("hf", "analyst+search")])
    r = await llm_client.ask_json("briefing", "sys", "user")
    assert called == [] and r.failures == []
