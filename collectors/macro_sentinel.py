"""
Groq-powered Macro Sentinel & Pre-Signal AI Reviewer.

Uses ultra-fast Groq Llama 3.3 (via async HTTP) to:
1. Conduct a rapid sanity check on trade candidates before dispatching signals
   (identifying crowded funding traps, liquidation exhaustion, or level conflicts).
2. Adjust confidence slightly (clamped [-0.04, +0.03]) without overriding mathematical rules.
3. Attach a plain-English AI review summary to the Telegram alert.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from collectors.llm_client import ask_json, chain_for
from config.settings import settings

if TYPE_CHECKING:
    from analysis.crypto_signal import CryptoSignal
    from analysis.crypto_state import CryptoState

log = structlog.get_logger()

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


def market_context(states, current: str, limit: int = 8) -> str:
    """
    The rest of the book, so the reviewer sees more than one chart.

    Deliberately only what is actually collected. Crude, the dollar index and
    the yield curve are not in this system — TwelveData refuses WTI on the
    current plan and nothing has ever fetched a bond. Naming them in the
    prompt would not make them appear; it would make the model supply them
    from memory, months stale, and that invented number would then move a
    confidence score. What is here is real and current.

    For a twenty-minute scalp this is also the more useful comparison: whether
    the book is moving together or the symbol is alone matters at that
    horizon, where the ten-year does not.
    """
    if not states:
        return "no other markets available"
    rows = []
    for st in states:
        if st.current_price <= 0:
            continue
        chg = ((st.current_price - st.price_24h_ago) / st.price_24h_ago * 100.0
               if getattr(st, "price_24h_ago", 0) else 0.0)
        tag = " <- this one" if st.symbol == current else ""
        rows.append(f"  {st.symbol.upper():<10} {chg:+6.2f}% 24h  "
                    f"RSI {st.rsi_14:>4.0f}  flow {st.cvd_trend or 'n/a'}{tag}")
    return "\n".join(rows[:limit]) if rows else "no other markets available"


class GroqSentinel:
    """
    The model's seat at the table, before a trade and after it.

    Still named for Groq because Groq is still what answers the pre-trade
    call, and renaming a class that six modules import earns nothing. What
    changed underneath is that the vendor is no longer written into the
    method: both calls go through the role router, so which provider serves
    them is configuration.
    """

    # 10 s, not 4: gpt-oss-120b with reasoning often needs 3-6 s, so a 4 s
    # budget turned many reviews into "no answer" or a fallback's one-liner.
    # The signal is repriced at the live price before it opens anyway.
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.last_factors = ""
        # Who actually answered the last pre-trade review, and how fast —
        # stored with the review instead of the admin's model setting, which
        # the router never read.
        self.last_model = ""
        self.last_latency_ms = 0

    @property
    def is_available(self) -> bool:
        """
        Whether any provider is configured for the pre-trade role.

        This used to ask specifically whether Groq had a key, which was the
        same question while Groq was the only account and is the wrong one
        now — it would report the reviewer as unavailable with three other
        providers sitting configured and idle.
        """
        return bool(chain_for("pre_trade"))

    @property
    def postmortem_available(self) -> bool:
        """The post-trade chain is configured separately and may differ."""
        return bool(chain_for("post_trade"))

    async def review_signal_candidate(
        self, sig: CryptoSignal, state: CryptoState, model: str | None = None,
        book: list | None = None, news: str = "",
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
            f"\nRest of the book right now:\n{market_context(book, sig.symbol)}\n"
            + (f"\nNews:\n{news}\n" if news else "")
        )

        system_prompt = (
            "You have traded crypto perpetuals for fifteen years and you were "
            "usually the one saying no.\n\n"
            "Work it out before you answer. Check the levels against the "
            "market's own volatility, check the flow and funding against the "
            "direction, check whether the rest of the book agrees. Then decide.\n\n"
            "Judge what is below. The news section is a web-searched briefing "
            "and scored headlines: weigh a war, a tariff or a rate decision "
            "against the direction, ignore routine noise, and never invent "
            "news that is not listed — guessing is worse than saying nothing.\n\n"
            "Most trades are fine; say so and move on. REJECT is for "
            "structurally broken, not merely dull.\n\n"
            f"Tags, use only these: {_FACTOR_HELP}\n\n"
            "JSON only:\n"
            '{"reasoning": "2 to 4 sentences: what you checked and what decided it", '
            '"verdict": "APPROVE|CAUTION|REJECT", '
            '"confidence_delta": -0.04 to 0.03, '
            '"factors": ["tag"], '
            '"summary": "one plain sentence for the trader, under 200 chars"}'
        )

        # This one IS on the clock. The signal it is reviewing is priced off a
        # market that keeps moving, so a thorough answer arriving ten seconds
        # late is worth less than a quick one now — hence the short timeout
        # and the fast end of the chain.
        reply = await ask_json("pre_trade", system_prompt, user_content,
                               max_tokens=900, temperature=0.2, timeout=self.timeout)
        if not reply:
            return 0.0, "", ""

        parsed = reply.data
        # Clamped hard. The model is a stakeholder in the decision, not the
        # decider: it can nudge a confidence score, never overturn the maths
        # that produced it.
        clamped_delta = max(-0.04, min(0.03, float(parsed.get("confidence_delta", 0.0) or 0.0)))
        # The reasoning used to be thrown away, leaving only a 120-character
        # one-liner to judge the reviewer by. Keep both.
        summary = str(parsed.get("summary", "")).strip()
        why = str(parsed.get("reasoning", "")).strip()
        if why:
            summary = (summary + " | why: " + why)[:900]
        verdict = str(parsed.get("verdict", "")).strip().upper()
        self.last_factors = _clean_factors(parsed.get("factors"), PRE_FACTORS)
        self.last_model = reply.served_by
        self.last_latency_ms = reply.latency_ms

        log.info("signal_reviewed", symbol=sig.symbol, verdict=verdict,
                 delta=clamped_delta, factors=self.last_factors, summary=summary,
                 served_by=reply.served_by)
        return clamped_delta, summary, verdict

    async def review_closed_trade(self, trade, pre_review: str = "",
                                  model: str | None = None, news: str = "") -> dict:
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
            + (f"\nNews around the trade:\n{news}\n" if news else "")
        )

        system_prompt = (
            "A trade you looked at earlier has closed. Work out whether the "
            "loss was bad luck or a bad decision — and whether the win was "
            "skill or a coin flip that landed your way.\n\n"
            "Think it through first: compare what was planned against what "
            "price did, then against what it cost. A winner that needed a gap "
            "is not an edge. A loss with no lesson is allowed — inventing one "
            "teaches the wrong thing.\n\n"
            "Use the news listed below, from around the time of the trade, "
            "to judge whether the world moved against it; if it did not, say "
            "the trade stood on its numbers. Never invent news.\n\n"
            f"Tags, use only these: {_POST_FACTOR_HELP}\n\n"
            "JSON only:\n"
            '{"reasoning": "what actually decided it, under 150 chars", '
            '"verdict": "GOOD_TRADE|BAD_LUCK|FLAWED_SETUP|LUCKY", '
            '"factors": ["tag"], '
            '"lesson": "one or two sentences as you would say them, under 200 chars"}'
        )

        # Generous token ceiling and no speed pressure: the trade is booked
        # and nothing is waiting on this. What it produces is a dataset row,
        # and a rushed row is worse than a slow one.
        reply = await ask_json("post_trade", system_prompt, user_content,
                               max_tokens=700, temperature=0.3, timeout=30.0)
        if not reply:
            return {}
        parsed = reply.data

        out = {
            "verdict": str(parsed.get("verdict", "")).strip().upper(),
            "factors": _clean_factors(parsed.get("factors"), POST_FACTORS),
            "summary": (str(parsed.get("lesson", "")).strip()
                        + (" | why: " + str(parsed.get("reasoning", "")).strip()
                           if parsed.get("reasoning") else ""))[:400],
            "model": reply.served_by,
            "latency_ms": reply.latency_ms,
        }
        log.info("trade_reviewed", symbol=trade.symbol, outcome=trade.exit_reason,
                 verdict=out["verdict"], factors=out["factors"], lesson=out["summary"],
                 served_by=reply.served_by)
        return out
