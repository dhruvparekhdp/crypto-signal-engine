"""
Unit tests for GroqSentinel AI sanity reviewer and pre-signal second opinion.
"""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState
from collectors.macro_sentinel import GroqSentinel, _pretrade_review_cache
from notifications.crypto_formatter import format_crypto_signal


@pytest.fixture(autouse=True)
def _clear_pretrade_review_cache():
    """The pre-trade review cache is a module-level singleton, so without
    this a review answered in one test could silently "answer" the next
    test's identical-looking signal instead of the mocked HTTP response
    that test set up."""
    _pretrade_review_cache.clear()
    yield
    _pretrade_review_cache.clear()


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

    # Test 1: Groq returns large positive delta -> 0: the reviewer can only veto
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
        assert delta == 0.0  # an approval never adds confidence
        assert "Clean breakout" in review
        assert verdict == "APPROVE"

    # Same signal, same price as test 1 above — without clearing the
    # pre-trade review cache here this would be served test 1's cached
    # APPROVE answer instead of actually hitting the mocked HTTP response
    # below, since both fall within the default cache window and price
    # tolerance.
    _pretrade_review_cache.clear()

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


def test_reject_penalty_stays_a_real_say_against_todays_lower_floors():
    """
    1 Oct: the owner's own floors had drifted down (paper_min_confidence and
    crypto_min_confidence often sit around 0.55-0.65 now, not the 0.70 this
    penalty was originally sized against), and the OLD fixed 0.15 penalty
    against a lower floor stopped being "a real say" and became "REJECT
    almost always kills it" - only a setup that started above ~0.75 could
    ever survive a REJECT at a 0.60 floor. The whole point of the paper
    engine is to generate data on whether a signal was actually good or
    bad; a signal discarded before it can open a trade never becomes that
    data. 0.08 restores the original ratio at today's floors: a setup that
    started reasonably strong (0.70) still gets a chance to open and prove
    the reviewer right or wrong, while a setup already close to the floor
    (0.62) still correctly does not survive.
    """
    from config.settings import settings
    penalty = settings.groq_reject_penalty
    floor = 0.60   # a realistic current paper_min_confidence, not the old 0.70

    def after(conf):
        return max(0.50, min(0.95, round(conf - penalty, 4)))

    assert after(0.70) >= floor, (
        "a setup that started reasonably strong must survive a REJECT at today's "
        "lower floors, so it can actually open and generate real outcome data")
    assert after(0.62) < floor, "a setup already close to the floor should still not survive"


# ── Pre-trade review cache: bounded staleness, not a blind time cache ──────
#
# Follows tests/test_cache.py::TestPaperNeverCachesMarkPrice's discipline:
# proving the cache does NOT go stale matters more than proving it caches
# at all, because a stale AI trade verdict is the failure mode that
# actually costs money.

def _mock_review_response(verdict="APPROVE", delta=0.0, summary="looks fine"):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": (
        f'{{"verdict": "{verdict}", "confidence_delta": {delta}, '
        f'"summary": "{summary}"}}'
    )}}]}
    return resp


@pytest.mark.asyncio
async def test_a_repeat_signal_within_price_tolerance_and_ttl_reuses_the_cached_answer():
    """Same symbol/direction/setup, price barely moved, well inside the TTL
    -> the second review must not touch Groq at all."""
    sentinel = GroqSentinel()
    sig = _make_signal()
    sig.current_price = 65000.0
    state = CryptoState(symbol="btcusdt", base_asset="BTC")
    state.current_price = 65000.0

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("collectors.macro_sentinel.settings.pre_trade_review_cache_ttl_seconds", 180), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_review_response(
            verdict="CAUTION", delta=-0.02, summary="funding is a bit crowded")

        first = await sentinel.review_signal_candidate(sig, state)
        assert mock_post.call_count == 1

        # A near-identical repeat, 0.05% away — well inside the 0.15% band.
        sig2 = _make_signal()
        sig2.current_price = 65000.0 * 1.0005
        state.current_price = sig2.current_price
        second = await sentinel.review_signal_candidate(sig2, state)

        # The real call was never made a second time...
        assert mock_post.call_count == 1, "a repeat within tolerance must not call Groq again"
        # ...and the cached answer, not a default/empty one, came back.
        assert second == first
        assert second[2] == "CAUTION"
        assert "funding is a bit crowded" in second[1]


@pytest.mark.asyncio
async def test_a_real_price_move_past_tolerance_is_never_served_from_cache():
    """A genuine move (> 0.15%) must always be a cache miss, however
    recently the last review ran — this is the guard against ever serving
    a stale verdict on a real change."""
    sentinel = GroqSentinel()
    sig = _make_signal()
    sig.current_price = 65000.0
    state = CryptoState(symbol="btcusdt", base_asset="BTC")
    state.current_price = 65000.0

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("collectors.macro_sentinel.settings.pre_trade_review_cache_ttl_seconds", 180), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_review_response(
            verdict="APPROVE", delta=0.0, summary="first look")
        first = await sentinel.review_signal_candidate(sig, state)
        assert mock_post.call_count == 1

        # A full 1% move — well past the 0.15% tolerance, seconds later.
        sig2 = _make_signal()
        sig2.current_price = 65000.0 * 1.01
        state.current_price = sig2.current_price
        mock_post.return_value = _mock_review_response(
            verdict="REJECT", delta=-0.04, summary="broke through the level")
        second = await sentinel.review_signal_candidate(sig2, state)

        assert mock_post.call_count == 2, "a real price move must always trigger a fresh call"
        assert second != first
        assert second[2] == "REJECT"
        assert "broke through the level" in second[1]


@pytest.mark.asyncio
async def test_a_repeat_past_the_ttl_is_never_served_from_cache():
    """Even with the price unchanged, an entry older than the TTL must be
    treated as a miss — the window is a hard bound, not a suggestion."""
    sentinel = GroqSentinel()
    sig = _make_signal()
    sig.current_price = 65000.0
    state = CryptoState(symbol="btcusdt", base_asset="BTC")
    state.current_price = 65000.0

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("collectors.macro_sentinel.settings.pre_trade_review_cache_ttl_seconds", 0.05), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_review_response(
            verdict="APPROVE", delta=0.0, summary="first look")
        await sentinel.review_signal_candidate(sig, state)
        assert mock_post.call_count == 1

        await asyncio.sleep(0.08)  # past the 0.05s TTL

        sig2 = _make_signal()
        sig2.current_price = 65000.0  # identical price — only time passed
        mock_post.return_value = _mock_review_response(
            verdict="APPROVE", delta=0.0, summary="second look, still fine")
        await sentinel.review_signal_candidate(sig2, state)

        assert mock_post.call_count == 2, "an expired entry must not be served from cache"


@pytest.mark.asyncio
async def test_cache_disabled_when_ttl_is_zero():
    """0 disables the cache outright, per its setting's own contract —
    every review is a real call."""
    sentinel = GroqSentinel()
    sig = _make_signal()
    sig.current_price = 65000.0
    state = CryptoState(symbol="btcusdt", base_asset="BTC")
    state.current_price = 65000.0

    with patch("collectors.macro_sentinel.settings.groq_api_key", SecretStr("mock-key")), \
         patch("collectors.macro_sentinel.settings.pre_trade_review_cache_ttl_seconds", 0), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = _mock_review_response()
        await sentinel.review_signal_candidate(sig, state)
        await sentinel.review_signal_candidate(sig, state)
        assert mock_post.call_count == 2
