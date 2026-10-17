"""18 Oct 2026 owner: "too many options, most of them are of no use and confusing". /settings shows a few main
sections, one collapsed Advanced section, and hides retired options - whose saved values must still apply."""
from config.overrides import ADVANCED, BY_KEY, FIELDS, MAIN_SECTIONS, apply, describe, tier_of
from config.settings import Settings


def test_every_main_and_advanced_key_is_a_real_field():
    for keys in MAIN_SECTIONS.values():
        for k in keys:
            assert k in BY_KEY, k
    for k in ADVANCED:
        assert k in BY_KEY, k


def test_the_page_is_short():
    visible = [f for f in FIELDS if tier_of(f.key, f.group) == "main"]
    assert len(visible) <= 15
    assert [k for k in MAIN_SECTIONS["Live ₹ · Delta India"]][:3] == ["delta_india_kill_switch", "delta_india_mode",
                                                                       "delta_india_live_orders"]


def test_retired_values_still_apply():
    s = Settings()
    assert tier_of("mirror_review_enabled", "signals") == "retired"
    applied = apply(s, {"mirror_review_enabled": False, "binance_klines_seconds": 30})
    assert "mirror_review_enabled" in applied and s.mirror_review_enabled is False and s.binance_klines_seconds == 30


def test_describe_carries_tier_section_and_rank():
    rows = {r["key"]: r for r in describe(Settings(), {})}
    assert rows["delta_india_kill_switch"]["tier"] == "main" and rows["delta_india_kill_switch"]["rank"] == 0
    assert rows["delta_india_base_url"]["tier"] == "advanced"
    assert rows["groq_api_key"]["tier"] == "key"
