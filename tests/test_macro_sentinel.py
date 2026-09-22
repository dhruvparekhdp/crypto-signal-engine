"""
Unit tests for GroqSentinel AI sanity reviewer and pre-signal second opinion.
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
from collectors.macro_sentinel import GroqSentinel
from notifications.crypto_formatter import format_crypto_signal


def _make_signal(ai_review: str = "") -> CryptoSignal:
    return CryptoSignal(
        symbol="btcusdt",
        signal_type="confluence",
        direction="long",
        trigger_description="Strong multi-indicator alignment",
        confidence=0.75,
        current_price=65000.0,
        target_price=66500.0,
        stop_loss=64250.0,
        edge_pct=2.3,
        stake_pct=0.015,
        timeframe="15m",
        sentiment_score=0.1,
        indicators_summary="RSI 48, MACD positive, HTF EMA above",
        timestamp=datetime.now(UTC),
        ai_review=ai_review,
    )


@pytest.mark.asyncio
async def test_groq_sentinel_disabled_without_key():
    sentinel = GroqSentinel()
    with patch("collectors.macro_sentinel.settings.groq_api_key", None):
        assert not sentinel.is_available
        state = CryptoState(symbol="btcusdt", base_asset="BTC")
        delta, review, verdict = await sentinel.review_signal_candidate(_make_signal(), state)
        assert delta == 0.0
        assert review == ""


@pytest.mark.asyncio
async def test_groq_sentinel_clamping_and_review():
    sentinel = GroqSentinel()
    sig = _make_signal()
    state = CryptoState(symbol="btcusdt", base_asset="BTC")
    state.current_price = 65000.0
    state.cvd_trend = "bullish_delta"

    # Test 1: Groq returns large positive delta -> clamped to +0.03
    mock_resp_positive = MagicMock()
    mock_resp_positive.status_code = 200
    mock_resp_positive.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"verdict": "APPROVE", "confidence_delta": 0.15, "summary": "Clean breakout with solid order flow alignment."}'
                }
            }
        ]
    }

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp_positive
        delta, review, verdict = await sentinel.review_signal_candidate(sig, state)
        assert delta == 0.03  # Clamped from 0.15 to +0.03
        assert "Clean breakout" in review
        assert verdict == "APPROVE"

    # Test 2: Groq returns large negative delta -> clamped to -0.04
    mock_resp_negative = MagicMock()
    mock_resp_negative.status_code = 200
    mock_resp_negative.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"verdict": "CAUTION", "confidence_delta": -0.10, "summary": "Crowded funding poses high squeeze risk."}'
                }
            }
        ]
    }

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp_negative
        delta, review, verdict = await sentinel.review_signal_candidate(sig, state)
        assert delta == -0.04  # Clamped from -0.10 to -0.04
        assert "Crowded funding" in review
        assert verdict == "CAUTION"


@pytest.mark.asyncio
async def test_a_reject_verdict_is_surfaced_so_it_can_carry_weight():
    """
    The gold signal that prompted this: the reviewer called a 43% target
    "mathematically impossible" and the signal published anyway, because a
    clamped +/-0.04 delta was too quiet to matter. The verdict now comes back
    so the caller can charge a REJECT real confidence — enough to sink a
    typical setup under the threshold, not enough to overrule a strong one.
    """
    sentinel = GroqSentinel()
    state = CryptoState(symbol="xauusdt", base_asset="XAU")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": (
        '{"verdict": "REJECT", "confidence_delta": -0.04, "summary": '
        '"Target is 43% away, making the 3:1 R:R mathematically impossible."}'
    )}}]}

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = resp
        delta, review, verdict = await sentinel.review_signal_candidate(_make_signal(), state)

    assert verdict == "REJECT"
    assert "mathematically impossible" in review


@pytest.mark.asyncio
async def test_groq_sentinel_handles_http_errors_gracefully():
    sentinel = GroqSentinel()
    sig = _make_signal()
    state = CryptoState(symbol="btcusdt", base_asset="BTC")

    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp_500
        delta, review, verdict = await sentinel.review_signal_candidate(sig, state)
        assert delta == 0.0
        assert review == ""


def test_telegram_message_includes_ai_review():
    sig = _make_signal(ai_review="CVD confirms buyer aggression, low squeeze resistance.")
    msg = format_crypto_signal(sig)
    assert "🤖 <b>AI Review (Groq Sentinel):</b>" in msg
    assert "CVD confirms buyer aggression" in msg


def test_a_reject_is_a_strong_opinion_not_a_veto():
    """
    Sized so the reviewer is heard without being obeyed. A ±0.04 nudge let a
    43%-target gold signal through; a hard veto would let one bad model call
    silently kill good setups. The penalty sinks an ordinary setup below the
    threshold and leaves a strong one standing, so the confidence threshold
    stays the single place a signal is refused.
    """
    from config.settings import settings
    penalty = settings.groq_reject_penalty
    threshold = 0.70

    def after(conf):
        return max(0.50, min(0.95, round(conf - penalty, 4)))

    assert after(0.74) < threshold, "a typical setup should not survive a REJECT"
    assert after(0.92) >= threshold, "a strong setup should outvote the reviewer"
