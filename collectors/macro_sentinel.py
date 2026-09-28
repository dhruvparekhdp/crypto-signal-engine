"""
Groq-powered Macro Sentinel & Pre-Signal AI Reviewer.

Uses ultra-fast Groq Llama 3.3 (via async HTTP) to:
1. Conduct a rapid sanity check on trade candidates before dispatching signals
   (identifying crowded funding traps, liquidation exhaustion, or level conflicts).
2. Adjust confidence slightly (clamped [-0.04, +0.03]) without overriding mathematical rules.
3. Attach a plain-English AI review summary to the Telegram alert.
"""
from __future__ import annotations

import time
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


# Relative tolerance for the pre-trade review cache: 0.15% of the last
# reviewed price, not a fixed dollar amount — BTC in the tens of thousands
# and XRP under a dollar need the same RELATIVE move to count as "the
# market actually changed", not the same absolute one.
_PRETRADE_CACHE_PRICE_TOLERANCE_PCT = 0.0015


class _PreTradeReviewCache:
    """
    In-process cache of the last real pre-trade review answer, keyed on
    (symbol, direction, signal_type) — not a blind time-based cache. A hit
    requires BOTH the entry to still be within its TTL AND the price now to
    be within a small relative tolerance of the price that was actually
    reviewed. A real move past that tolerance is always a miss, however
    fresh the entry, because a moved market is a different question than
    the one that was already answered — this must never serve a stale
    verdict on a genuine change.

    Scoped deliberately narrow: only collectors/macro_sentinel.py's
    review_signal_candidate (the pre_trade role) reads or writes it.
    Nothing live/real-time-sensitive (position review, anything reading
    live P&L) touches this cache.
    """

    def __init__(self) -> None:
        # key -> (cached_at monotonic, cached_price, (delta, summary, verdict), factors)
        self._store: dict[tuple[str, str, str], tuple[float, float, tuple, str]] = {}

    def get(self, key: tuple[str, str, str], price: float, ttl: float
           ) -> tuple[tuple[float, str, str], str, float] | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        cached_at, cached_price, answer, factors = hit
        if cached_price <= 0:
            return None
        age = time.monotonic() - cached_at
        if age > ttl:
            return None
        if abs(price - cached_price) / cached_price > _PRETRADE_CACHE_PRICE_TOLERANCE_PCT:
            return None
        return answer, factors, age

    def set(self, key: tuple[str, str, str], price: float,
            answer: tuple[float, str, str], factors: str) -> None:
        self._store[key] = (time.monotonic(), price, answer, factors)

    def clear(self) -> None:
        """Test-only: the cache is process-local and otherwise never needs
        clearing in production — a stale entry ages out or falls outside
        tolerance on its own."""
        self._store.clear()


_pretrade_review_cache = _PreTradeReviewCache()


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
        # Round-0 mirror pair only: the per-candidate factor tags from the
        # one combined call, in (primary, mirror) order. Empty for every
        # other path, including the single-candidate review above.
        self.last_factors_pair: tuple[str, str] = ("", "")

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

    @staticmethod
    def _candidate_facts(sig: CryptoSignal, state: CryptoState) -> dict:
        """
        The per-candidate numbers a review needs: the chart read for THIS
        direction (a long and a short read the same candles differently)
        and the entry/target/stop/R:R maths. Pulled out so the combined
        round-0 pair review can compute it once per side without
        duplicating the single-candidate method's logic.
        """
        from analysis.price_action import describe as chart_text
        from analysis.price_action import pick_higher_timeframe
        higher_tf = pick_higher_timeframe()
        chart = chart_text(state.get_candles("5m"), state.get_candles(higher_tf),
                           sig.current_price, is_long=sig.direction.lower() == "long",
                           higher_tf=higher_tf)
        target_move = (abs(sig.target_price - sig.current_price) / sig.current_price * 100.0
                       if sig.target_price else 0.0)
        stop_move = (abs(sig.current_price - sig.stop_loss) / sig.current_price * 100.0
                     if sig.stop_loss else 0.0)
        rr = target_move / stop_move if stop_move > 0 else 0.0
        return {"chart": chart, "target_move": target_move, "stop_move": stop_move, "rr": rr}

    @staticmethod
    def _levels_block(sig: CryptoSignal, facts: dict) -> str:
        """The direction/setup/levels/chart lines for one candidate."""
        return (
            f"Direction: {sig.direction.upper()}\n"
            f"Setup: {sig.signal_type} ({sig.trigger_description})\n"
            f"Levels: Entry ${sig.current_price:,.4f}, "
            f"Target ${sig.target_price or 0:,.4f} (+{facts['target_move']:.2f}%), "
            f"Stop ${sig.stop_loss or 0:,.4f} (-{facts['stop_move']:.2f}%), "
            f"R:R {facts['rr']:.1f}\n"
            + (f"\nChart (read from the candles):\n{facts['chart']}\n" if facts["chart"] else "")
        )

    @staticmethod
    def _market_block(state: CryptoState) -> str:
        """The funding/OI/sentiment lines, identical for every candidate on
        the same symbol at the same moment — computed once and shared."""
        funding_str = (f"{state.funding_rate_per_8h * 100:+.3f}%"
                       if state.funding_rate_per_8h is not None else "neutral")
        oi_str = f"{state.oi_change_1h_pct:+.1f}% in 1h" if state.oi_change_1h_pct else "stable"
        return (
            f"Order Flow / CVD: {state.cvd_trend}\n"
            f"Derivatives: Funding {funding_str}, Open Interest {oi_str}\n"
            f"Sentiment: {state.sentiment_score:+.2f}\n"
        )

    @staticmethod
    def _parse_verdict(side: dict) -> tuple[float, str, str]:
        """One candidate's slice of a review reply, parsed the same way
        regardless of whether it came from a solo call or a paired one."""
        if not isinstance(side, dict):
            return 0.0, "", ""
        try:
            raw_delta = float(side.get("confidence_delta", 0.0) or 0.0)
        except (TypeError, ValueError):
            raw_delta = 0.0
        clamped = max(-0.04, min(0.0, raw_delta)) if raw_delta == raw_delta else 0.0
        summary = str(side.get("summary", "")).strip()
        why = str(side.get("reasoning", "")).strip()
        if why:
            summary = (summary + " | why: " + why)[:900]
        verdict = str(side.get("verdict", "")).strip().upper()
        return clamped, summary, verdict

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

        Checks a short in-process cache first (keyed on symbol, direction
        and signal_type, and only a hit while the price is within a small
        relative tolerance of what was actually reviewed): a near-identical
        repeat signal minutes later reuses the last real answer instead of
        spending another call. A genuine price move, or the TTL elapsing,
        is always a miss — see _PreTradeReviewCache.
        """
        # Reset first: a failed call must not be logged under the previous
        # call's model (the 22-25 Sep log showed empty answers recorded as
        # llama-3.3-70b at 0 ms).
        self.last_model = ""
        self.last_latency_ms = 0
        self.last_factors = ""
        if not self.is_available or not getattr(settings, "groq_signal_review_enabled", True):
            return 0.0, "", ""

        cache_key = (sig.symbol.lower(), sig.direction.lower(), sig.signal_type)
        cache_ttl = getattr(settings, "pre_trade_review_cache_ttl_seconds", 0) or 0
        if cache_ttl > 0:
            hit = _pretrade_review_cache.get(cache_key, sig.current_price, cache_ttl)
            if hit is not None:
                answer, factors, age = hit
                delta, summary, verdict = answer
                self.last_factors = factors
                # Honest about it: this was not a fresh model answer, and
                # save_review's `model` column must say so rather than
                # silently reusing whatever the last real call recorded.
                self.last_model = "cache"
                self.last_latency_ms = 0
                log.info("pre_trade_review_cache_hit", symbol=sig.symbol,
                         direction=sig.direction, signal_type=sig.signal_type,
                         age_s=round(age, 1), verdict=verdict)
                return delta, summary, verdict

        facts = self._candidate_facts(sig, state)

        user_content = (
            f"Symbol: {sig.symbol.upper()}\n"
            f"{self._levels_block(sig, facts)}"
            f"{self._market_block(state)}"
            + f"\nRest of the book right now:\n{market_context(book, sig.symbol)}\n"
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
            "Read the chart section the way a discretionary trader reads a "
            "5m/15m chart: a long into nearby resistance, a short into nearby "
            "support, or any trade against the 15m structure (lower highs and "
            "lows for a long, higher ones for a short) is at best CAUTION, and "
            "REJECT when the target sits beyond the level.\n\n"
            "Most trades are fine; say so and move on. REJECT is for "
            "structurally broken, not merely dull.\n\n"
            f"Tags, use only these: {_FACTOR_HELP}\n\n"
            "JSON only:\n"
            '{"reasoning": "2 to 4 sentences: what you checked and what decided it", '
            '"verdict": "APPROVE|CAUTION|REJECT", '
            '"confidence_delta": -0.04 to 0.0, '
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
        # And it can only say no (research: LLMs agree with confident-looking
        # setups and are a coin flip on direction). An approval adds nothing;
        # caution and rejection subtract.
        clamped_delta, summary, verdict = self._parse_verdict(parsed)
        self.last_factors = _clean_factors(parsed.get("factors"), PRE_FACTORS)
        self.last_model = reply.served_by
        self.last_latency_ms = reply.latency_ms

        log.info("signal_reviewed", symbol=sig.symbol, verdict=verdict,
                 delta=clamped_delta, factors=self.last_factors, summary=summary,
                 served_by=reply.served_by)
        if cache_ttl > 0:
            _pretrade_review_cache.set(cache_key, sig.current_price,
                                       (clamped_delta, summary, verdict), self.last_factors)
        return clamped_delta, summary, verdict

    async def review_signal_pair(
        self, primary_sig: CryptoSignal, mirror_sig: CryptoSignal, state: CryptoState,
        model: str | None = None, book: list | None = None, news: str = "",
    ) -> tuple[tuple[float, str, str], tuple[float, str, str]]:
        """
        Round-0 of mirror review, in one Groq call instead of two.

        A variant of review_signal_candidate's own system prompt — same
        reviewer persona, same "usually the one saying no", same
        anti-hallucination discipline on the news section — restructured to
        hold a primary and its mirror (opposite direction, same symbol,
        same moment) apart in the same call and ask for an independent
        verdict on each, so neither's framing leaks into the other's. Not
        used for the live re-review loop (_mirror_review_job): that path
        does not always have both sides still tracking to pair up, so it
        keeps calling review_signal_candidate one at a time.

        Reuses review_signal_candidate's own context-building helpers
        (_candidate_facts, _levels_block, _market_block, _parse_verdict)
        rather than duplicating them, and deliberately does NOT consult the
        pre-trade cache — round-0 candidates are, by definition, brand new.

        Returns ((delta, summary, verdict) for primary, (...) for mirror) —
        the exact shape review_signal_candidate returns for one candidate,
        so a caller that already knows how to use one candidate's result
        needs only unpack this into two.
        """
        self.last_model = ""
        self.last_latency_ms = 0
        self.last_factors = ""
        self.last_factors_pair = ("", "")
        empty = (0.0, "", "")
        if not self.is_available or not getattr(settings, "groq_signal_review_enabled", True):
            return empty, empty

        facts_p = self._candidate_facts(primary_sig, state)
        facts_m = self._candidate_facts(mirror_sig, state)

        user_content = (
            f"Symbol: {primary_sig.symbol.upper()}\n"
            f"{self._market_block(state)}"
            + f"\nRest of the book right now:\n{market_context(book, primary_sig.symbol)}\n"
            + (f"\nNews:\n{news}\n" if news else "")
            + f"\n--- PRIMARY candidate ---\n{self._levels_block(primary_sig, facts_p)}"
            + f"\n--- MIRROR candidate (opposite direction, same setup) ---\n"
            f"{self._levels_block(mirror_sig, facts_m)}"
        )

        system_prompt = (
            "You have traded crypto perpetuals for fifteen years and you were "
            "usually the one saying no.\n\n"
            "Below are TWO candidates on the same symbol at the same moment, "
            "pointing opposite directions: PRIMARY and MIRROR. Judge each one "
            "entirely on its own — a long into resistance and a short into "
            "support are not a package deal, and one candidate looking clean "
            "must never make you lean harder for, or against, the other. "
            "Work each one out separately before you answer either.\n\n"
            "For each candidate: check its levels against the market's own "
            "volatility, check the flow and funding against THAT candidate's "
            "own direction, check whether the rest of the book agrees. Then "
            "decide — independently.\n\n"
            "Judge what is below. The news section is a web-searched briefing "
            "and scored headlines: weigh a war, a tariff or a rate decision "
            "against each direction on its own terms, ignore routine noise, "
            "and never invent news that is not listed — guessing is worse "
            "than saying nothing.\n\n"
            "Read each chart section the way a discretionary trader reads a "
            "5m/15m chart: a long into nearby resistance, a short into nearby "
            "support, or any trade against the 15m structure (lower highs and "
            "lows for a long, higher ones for a short) is at best CAUTION, and "
            "REJECT when the target sits beyond the level.\n\n"
            "Most trades are fine; say so and move on. REJECT is for "
            "structurally broken, not merely dull. It is entirely normal for "
            "one candidate to be fine and the other broken, or for both to be "
            "fine, or both broken — let the evidence for each one decide it, "
            "not the other's verdict.\n\n"
            f"Tags, use only these: {_FACTOR_HELP}\n\n"
            "JSON only, with a fully independent verdict for each side:\n"
            '{"primary": {"reasoning": "2 to 4 sentences: what you checked and '
            'what decided it", "verdict": "APPROVE|CAUTION|REJECT", '
            '"confidence_delta": -0.04 to 0.0, "factors": ["tag"], '
            '"summary": "one plain sentence for the trader, under 200 chars"}, '
            '"mirror": {"reasoning": "...", "verdict": "APPROVE|CAUTION|REJECT", '
            '"confidence_delta": -0.04 to 0.0, "factors": ["tag"], '
            '"summary": "..."}}'
        )

        # Same time pressure as the single-candidate call, and the same
        # short timeout — this is still one call on the clock, just judging
        # two directions instead of one, so it gets a larger token budget
        # rather than a longer deadline.
        reply = await ask_json("pre_trade", system_prompt, user_content,
                               max_tokens=1500, temperature=0.2, timeout=self.timeout)
        if not reply:
            return empty, empty

        parsed = reply.data
        primary_raw = parsed.get("primary") if isinstance(parsed.get("primary"), dict) else {}
        mirror_raw = parsed.get("mirror") if isinstance(parsed.get("mirror"), dict) else {}

        delta_p, summary_p, verdict_p = self._parse_verdict(primary_raw)
        delta_m, summary_m, verdict_m = self._parse_verdict(mirror_raw)
        factors_p = _clean_factors(primary_raw.get("factors"), PRE_FACTORS)
        factors_m = _clean_factors(mirror_raw.get("factors"), PRE_FACTORS)

        self.last_model = reply.served_by
        self.last_latency_ms = reply.latency_ms
        self.last_factors_pair = (factors_p, factors_m)

        log.info("signal_pair_reviewed", symbol=primary_sig.symbol,
                 primary_verdict=verdict_p, mirror_verdict=verdict_m,
                 primary_delta=delta_p, mirror_delta=delta_m,
                 served_by=reply.served_by)
        return (delta_p, summary_p, verdict_p), (delta_m, summary_m, verdict_m)

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
