"""AI revamp phase A (15 Oct 2026 bake-off, data/lab/ai_bakeoff.json): chains hold only models that answered, and the
headlines that could pause trading get one batched LLM check on top of FinBERT."""
from analysis.headline_rules import apply_review, needs_review
from collectors.hermes import EVENT_TYPES, HIGH_IMPACT
from config.settings import Settings

BROKEN = ("hf:analyst", "hf:meta-llama/Llama-3.1-8B-Instruct", "openrouter:google/gemma-4-31b-it:free",
          "thinkingmachines/inkling", "nemotron-3-super")


def test_no_chain_uses_a_model_that_failed_the_bakeoff():
    s = Settings()
    for name in Settings.model_fields:
        if name.startswith("llm_chain_"):
            chain = getattr(s, name)
            for bad in BROKEN:
                assert bad not in chain, f"{name} still uses {bad}"


def test_research_is_not_opus():
    assert "opus" not in Settings().llm_chain_research


def test_only_high_impact_or_strong_headlines_are_checked():
    assert needs_review({"event_type": "inflation_data", "score": 0.0}, HIGH_IMPACT)
    assert needs_review({"event_type": "crypto_other", "score": -0.8}, HIGH_IMPACT)
    assert not needs_review({"event_type": "crypto_other", "score": 0.2}, HIGH_IMPACT)


def test_review_turns_false_alarms_into_noise_and_keeps_real_events():
    items = [{"headline": "Federal Reserve Board announces approval of application by X Bank", "score": 0.6,
              "confidence": 0.9, "event_type": "rate_decision", "model": "hf/finbert+rules"},
             {"headline": "Fed cuts rates by 50bp", "score": 0.3, "confidence": 0.6, "event_type": "rate_decision",
              "model": "hf/finbert+rules"}]
    n = apply_review(items, [{"i": 1, "relevant": False, "score": 0.3, "confidence": 0.9, "event_type": "noise"},
                             {"i": 2, "relevant": True, "score": 0.7, "confidence": 0.9, "event_type": "rate_decision"},
                             {"i": 9, "relevant": True, "score": 1}, "junk"], EVENT_TYPES)
    assert n == 2
    assert items[0]["event_type"] == "noise" and items[0]["score"] == 0.0
    assert items[1]["score"] == 0.7 and items[1]["model"].endswith("+llm")


def test_malformed_reply_keeps_the_finbert_score():
    items = [{"score": -0.9, "confidence": 0.8, "event_type": "hack", "model": "hf/finbert+rules"}]
    assert apply_review(items, [{"i": "x"}], EVENT_TYPES) == 0 and items[0]["score"] == -0.9
