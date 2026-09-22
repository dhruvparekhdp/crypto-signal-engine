"""
Groq-powered Macro Sentinel & Pre-Signal AI Reviewer.

Uses ultra-fast Groq Llama 3.3 (via async HTTP) to:
1. Conduct a rapid sanity check on trade candidates before dispatching signals
   (identifying crowded funding traps, liquidation exhaustion, or level conflicts).
2. Adjust confidence slightly (clamped [-0.04, +0.03]) without overriding mathematical rules.
3. Attach a plain-English AI review summary to the Telegram alert.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from config.settings import settings

if TYPE_CHECKING:
    from analysis.crypto_signal import CryptoSignal
    from analysis.crypto_state import CryptoState

log = structlog.get_logger()

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


class GroqSentinel:
    """Async client for Groq-powered sanity review and macro regime assessment."""

    def __init__(self, timeout: float = 4.0) -> None:
        self.timeout = timeout

    @property
    def is_available(self) -> bool:
        return bool(settings.groq_api_key and settings.groq_api_key.get_secret_value())

    async def review_signal_candidate(
        self, sig: CryptoSignal, state: CryptoState, model: str | None = None
    ) -> tuple[float, str, str]:
        """
        Pre-signal second opinion.

        Returns:
            (confidence_delta, review_summary, verdict)
            confidence_delta is strictly clamped to [-0.04, +0.03]

        The verdict is returned rather than folded into the delta because a
        clamped nudge cannot express "this trade is impossible". It said
        exactly that about a gold signal with a 43% target — "mathematically
        impossible and structurally unsound" — and the signal published
        anyway, because 0.04 of confidence was all it was allowed to move.
        """
        if not self.is_available or not getattr(settings, "groq_signal_review_enabled", True):
            return 0.0, "", ""

        api_key = settings.groq_api_key.get_secret_value()  # type: ignore[union-attr]
        active_model = model or getattr(settings, "groq_model", "qwen/qwen3.8-27b")

        # Context payload
        funding_str = (f"{state.funding_rate_per_8h * 100:+.3f}%"
                       if state.funding_rate_per_8h is not None else "neutral")
        oi_str = f"{state.oi_change_1h_pct:+.1f}% in 1h" if state.oi_change_1h_pct else "stable"
        cvd_str = state.cvd_trend

        target_move = (abs(sig.target_price - sig.current_price) / sig.current_price * 100.0
                       if sig.target_price else 0.0)
        stop_move = (abs(sig.current_price - sig.stop_loss) / sig.current_price * 100.0
                     if sig.stop_loss else 0.0)
        rr = target_move / stop_move if stop_move > 0 else 0.0

        user_content = (
            f"Symbol: {sig.symbol.upper()}\n"
            f"Direction: {sig.direction.upper()}\n"
            f"Setup: {sig.signal_type} ({sig.trigger_description})\n"
            f"Levels: Entry ${sig.current_price:,.4f}, Target ${sig.target_price or 0:,.4f} (+{target_move:.2f}%), Stop ${sig.stop_loss or 0:,.4f} (-{stop_move:.2f}%), R:R {rr:.1f}\n"
            f"Order Flow / CVD: {cvd_str}\n"
            f"Derivatives: Funding {funding_str}, Open Interest {oi_str}\n"
            f"Sentiment: {state.sentiment_score:+.2f}\n"
        )

        system_prompt = (
            "You are a quantitative crypto risk sentinel reviewing a proposed intraday trade.\n"
            "Your objective is a quick sanity check to catch blatant traps:\n"
            "- Crowded funding against the position (e.g. going short when shorts pay heavy funding)\n"
            "- Aggressive short covering or long liquidation exhaustion\n"
            "- Extreme resistance/support level friction\n"
            "Do NOT attempt to predict the future. Assess whether the risk/reward and order flow make sense.\n"
            "Respond ONLY with a JSON object:\n"
            "{\n"
            '  "verdict": "APPROVE" | "CAUTION" | "REJECT",\n'
            '  "confidence_delta": float (-0.04 to +0.03),\n'
            '  "summary": "1 concise sentence (under 120 chars) summarizing your assessment"\n'
            "}"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 150,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(GROQ_ENDPOINT, json=payload, headers=headers)
                if resp.status_code in (400, 404) and payload["model"] != "qwen/qwen3.8-27b":
                    payload["model"] = "qwen/qwen3.8-27b"
                    resp = await client.post(GROQ_ENDPOINT, json=payload, headers=headers)

                if resp.status_code != 200:
                    log.warning("groq_review_failed", status=resp.status_code)
                    return 0.0, "", ""

                res_json = resp.json()
                content_str = res_json["choices"][0]["message"]["content"]
                parsed = json.loads(content_str)

                delta = float(parsed.get("confidence_delta", 0.0))
                # Strictly clamp influence
                clamped_delta = max(-0.04, min(0.03, delta))
                summary = str(parsed.get("summary", "")).strip()
                verdict = str(parsed.get("verdict", "")).strip().upper()

                log.info("groq_signal_reviewed", symbol=sig.symbol, verdict=verdict,
                         delta=clamped_delta, summary=summary)
                return clamped_delta, summary, verdict

        except Exception as exc:
            log.debug("groq_review_exception", symbol=sig.symbol, error=str(exc))
            return 0.0, "", ""
