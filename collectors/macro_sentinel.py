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
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from config.settings import settings

if TYPE_CHECKING:
    from analysis.crypto_signal import CryptoSignal
    from analysis.crypto_state import CryptoState

log = structlog.get_logger()

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
FALLBACK_MODEL = "qwen/qwen3.8-27b"

# A closed vocabulary, because free text cannot be counted. "The stop was a
# little tight given how the market was moving" and "stop inside noise" mean
# the same thing and aggregate to nothing. These do aggregate: after a week
# you can ask how many losses carried stop_inside_noise and get a number.
PRE_FACTORS = [
    "crowded_funding", "oi_piled_in", "flow_against", "thin_liquidity",
    "level_friction", "against_higher_timeframe", "overextended",
    "stop_inside_noise", "target_out_of_reach", "weak_volume",
    "clean_structure", "strong_confluence", "flow_confirms", "nothing_wrong",
]
POST_FACTORS = [
    "stopped_by_noise", "reversed_after_entry", "never_moved",
    "target_hit_cleanly", "trailed_out_early", "costs_ate_the_edge",
    "slippage_hurt", "held_too_long", "right_idea_wrong_level",
    "right_for_the_wrong_reason", "setup_was_flawed", "worked_as_designed",
]
_FACTOR_HELP = ", ".join(PRE_FACTORS)
_POST_FACTOR_HELP = ", ".join(POST_FACTORS)


def _clean_factors(raw, allowed: list[str]) -> str:
    """Keep only labels from the vocabulary; a model inventing its own is noise."""
    if not isinstance(raw, list):
        return ""
    keep = [str(f).strip().lower() for f in raw]
    return ",".join([f for f in keep if f in allowed][:4])


class GroqSentinel:
    """Async client for Groq-powered sanity review and macro regime assessment."""

    def __init__(self, timeout: float = 4.0) -> None:
        self.timeout = timeout
        self.last_factors = ""

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
            "You have traded crypto perpetuals for fifteen years, and for most of "
            "them you were the person who said no.\n\n"
            "Someone on your desk is about to take the trade below. They want your "
            "read before they click. You are not forecasting where price goes — "
            "you are answering one question: is there something here that makes "
            "this a worse trade than it looks on the card?\n\n"
            "What actually catches people:\n"
            "- Funding crowded against the position. Everyone is already on this "
            "side and paying rent to stay there.\n"
            "- Open interest that piled in on the way to this level, so the move "
            "has already been paid for.\n"
            "- Aggressive flow leaning the other way while price holds up. Someone "
            "is being filled into strength.\n"
            "- A stop parked inside the market's ordinary noise, or a target that "
            "needs a move this market does not make in the time allowed.\n\n"
            "Most trades are fine. Say so plainly and move on — a reviewer who "
            "flags everything is worth nothing to anybody. Save REJECT for when "
            "the trade is structurally broken, not merely unexciting.\n\n"
            "Write the summary the way you would say it across the desk: one "
            "sentence, plain words, no hedging and no throat-clearing.\n\n"
            "Tag what you saw using only these labels:\n"
            f"{_FACTOR_HELP}\n\n"
            "Reply with JSON only:\n"
            "{\n"
            '  "verdict": "APPROVE" | "CAUTION" | "REJECT",\n'
            '  "confidence_delta": float between -0.04 and +0.03,\n'
            '  "factors": ["label", ...],\n'
            '  "summary": "one sentence, under 120 characters"\n'
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
                if resp.status_code in (400, 404) and payload["model"] != FALLBACK_MODEL:
                    payload["model"] = FALLBACK_MODEL
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
                self.last_factors = _clean_factors(parsed.get("factors"), PRE_FACTORS)

                log.info("groq_signal_reviewed", symbol=sig.symbol, verdict=verdict,
                         delta=clamped_delta, factors=self.last_factors, summary=summary)
                return clamped_delta, summary, verdict

        except Exception as exc:
            log.debug("groq_review_exception", symbol=sig.symbol, error=str(exc))
            return 0.0, "", ""

    async def review_closed_trade(self, trade, pre_review: str = "",
                                  model: str | None = None) -> dict:
        """
        The post-mortem, once the trade is done and the answer is known.

        This is the half that makes the pre-trade review worth anything. On
        its own a verdict is an opinion nobody ever grades; paired with what
        actually happened it becomes a record you can hold the reviewer to —
        and, after enough of them, a labelled dataset for deciding which
        setups deserve to fire at all.

        Deliberately uses a slower, larger model than the pre-trade pass. No
        signal is waiting on this, so there is no reason to buy speed.
        """
        if not self.is_available:
            return {}

        api_key = settings.groq_api_key.get_secret_value()  # type: ignore[union-attr]
        active_model = model or settings.groq_postmortem_model

        move_pct = ((trade.exit_price - trade.entry_price) / trade.entry_price * 100.0
                    if trade.entry_price else 0.0)
        if trade.side == "short":
            move_pct = -move_pct
        planned = (abs(trade.target_price - trade.entry_price) / trade.entry_price * 100.0
                   if trade.entry_price and trade.target_price else 0.0)
        risked = (abs(trade.entry_price - trade.stop_price) / trade.entry_price * 100.0
                  if trade.entry_price and trade.stop_price else 0.0)

        user_content = (
            f"{trade.symbol.upper()} {trade.side.upper()} — {trade.exit_reason}\n"
            f"Setup: {trade.signal_type} at {round(trade.confidence * 100)}% confidence\n"
            f"Planned: target {planned:.3f}% away, stop {risked:.3f}% away\n"
            f"Happened: price moved {move_pct:+.3f}% in your favour before it closed\n"
            f"Held for {trade.hours_held:.2f} hours\n"
            f"Money: gross {trade.gross_pnl:+.2f}, fees {trade.trading_fees:.2f}, "
            f"funding {trade.funding_paid:.2f}, net {trade.net_pnl:+.2f} "
            f"({trade.return_on_margin * 100:+.1f}% on margin)\n"
            + (f"What you said before the trade: \"{pre_review}\"\n" if pre_review else "")
        )

        system_prompt = (
            "A trade you looked at earlier has closed. You are doing the thing "
            "good desks do at the end of the day — working out whether a loss "
            "was bad luck or a bad decision, and whether a win was skill or a "
            "coin flip that happened to land your way.\n\n"
            "Be honest about the winners too. A trade that made money because "
            "the market gapped in your favour is not an edge you can repeat, "
            "and saying so now is worth more than the profit was.\n\n"
            "Things worth naming:\n"
            "- Was the stop somewhere the market was always going to reach on "
            "ordinary noise?\n"
            "- Did it simply never move, and the clock closed it? Then the "
            "horizon was wrong, not the direction.\n"
            "- Did fees and funding take more than the idea was ever going to "
            "make?\n"
            "- Was the direction right but the entry level careless?\n\n"
            "If the trade was fine and the market just went the other way, say "
            "that. Not every loss has a lesson, and inventing one teaches the "
            "wrong thing.\n\n"
            "Write like you are talking, not filing a report. One or two "
            "sentences.\n\n"
            "Tag it using only these labels:\n"
            f"{_POST_FACTOR_HELP}\n\n"
            "Reply with JSON only:\n"
            "{\n"
            '  "verdict": "GOOD_TRADE" | "BAD_LUCK" | "FLAWED_SETUP" | "LUCKY",\n'
            '  "factors": ["label", ...],\n'
            '  "lesson": "one or two sentences, under 200 characters"\n'
            "}"
        )

        payload: dict[str, Any] = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_tokens": 250,
        }
        headers = {"Authorization": f"Bearer {api_key}",
                   "Content-Type": "application/json"}

        started = time.monotonic()
        try:
            # Generous timeout: nothing is blocked on this, and a post-mortem
            # that gives up early is worth less than one that waits.
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(GROQ_ENDPOINT, json=payload, headers=headers)
                if resp.status_code in (400, 404) and active_model != FALLBACK_MODEL:
                    payload["model"] = FALLBACK_MODEL
                    active_model = FALLBACK_MODEL
                    resp = await client.post(GROQ_ENDPOINT, json=payload, headers=headers)
                if resp.status_code != 200:
                    log.warning("groq_postmortem_failed", status=resp.status_code,
                                symbol=trade.symbol)
                    return {}
                parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            log.debug("groq_postmortem_exception", symbol=trade.symbol, error=str(exc))
            return {}

        out = {
            "verdict": str(parsed.get("verdict", "")).strip().upper(),
            "factors": _clean_factors(parsed.get("factors"), POST_FACTORS),
            "summary": str(parsed.get("lesson", "")).strip()[:400],
            "model": active_model,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        log.info("groq_trade_reviewed", symbol=trade.symbol, outcome=trade.exit_reason,
                 verdict=out["verdict"], factors=out["factors"], lesson=out["summary"])
        return out
