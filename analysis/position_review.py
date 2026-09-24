"""
Whether an open position still deserves to be open.

The rule, in the operator's words: a trade at a loss is held only on 75%
confidence, and a trade in profit is trailed by confidence rather than held
on faith. Both halves only ever tighten risk — a losing position can be
closed EARLIER than its stop, never later, and a winner's trail can only
ratchet toward price. Nothing here can move a stop away or defer one that
price has already reached.

That last point is the whole safety argument and it is worth being explicit
about. By the time a stop fires, price has traded through the level; holding
past it is not patience, it is an unbounded loss wearing patience's clothes.
So this runs only on positions that SURVIVED the tick's exit check.

Where the confidence comes from
-------------------------------
Two inputs, weighted very unevenly on purpose.

The measurable part is computed here from the market state, costs nothing,
and runs on every tick: is the momentum still on the position's side, is
there room left before the stop, has the trade used its time without going
anywhere. The model's part is a bounded adjustment on top — the same
stakeholder-not-decider shape the pre-trade reviewer already uses, because a
model that can veto on its own becomes the strategy.

And the model is only consulted when its answer could change the decision.
Below 0.65 no possible adjustment reaches the threshold, and at 0.90 or above
no possible adjustment falls below it, so those cases are settled locally and
for free. Paying for an opinion that cannot change the outcome is not
diligence, it is latency and money.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

log = structlog.get_logger()

# What the model may move the local read by. Asymmetric: it is given more
# room to argue a position should be closed than to argue it should be held,
# because the instruction was to keep losses minimal and the failure modes
# are not symmetric either.
AI_MAX_HELP = 0.10
AI_MAX_HARM = 0.15

# Below this, no adjustment the model can make reaches the hold threshold.
# Above the second, none takes it below. Both are derived from the bounds
# above and the threshold, not chosen separately — see `_needs_model`.
HOLD_THRESHOLD = 0.75


@dataclass(frozen=True)
class Review:
    """What was decided, and on what."""

    hold: bool
    confidence: float
    trend: float
    reason: str
    asked_model: bool = False
    factors: str = ""
    summary: str = ""

    @property
    def verdict(self) -> str:
        return "hold" if self.hold else "close"


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def momentum_agrees(state, is_long: bool) -> float:
    """
    Is the short-term push still on this position's side?

    MACD histogram rather than the lines: the histogram is the rate of change,
    which turns before the crossover does, and a position at a loss cannot
    afford to wait for the crossover to confirm what it already suspects.
    """
    hist = getattr(state, "macd_histogram", 0.0) or 0.0
    if hist == 0.0:
        return 0.5
    favourable = hist > 0 if is_long else hist < 0
    return 0.75 if favourable else 0.25


def not_exhausted(state, is_long: bool) -> float:
    """
    Is there room left in the direction the position needs?

    A long at RSI 82 needs the market to push further into territory it has
    already struggled in. This is not a reversal signal — it is the absence of
    a reason to expect more of the same.
    """
    rsi = getattr(state, "rsi_14", 50.0) or 50.0
    room = (100.0 - rsi) if is_long else rsi
    # 50 points of room is unremarkable; 20 or less is a position out of road.
    return _clamp01(room / 50.0)


def room_before_stop(entry: float, price: float, stop: float, is_long: bool) -> float:
    """
    How much of the original risk is still unspent.

    1.0 at entry, 0.0 at the stop. A position that has spent four fifths of
    its risk is not a position with a thesis, it is a position with a stop
    about to fire, and holding it is a bet that the last fifth behaves
    differently from the first four.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.5
    spent = (entry - price) if is_long else (price - entry)
    return _clamp01(1.0 - spent / risk)


def time_used(opened_at: datetime, expires_at: datetime | None, now: datetime) -> float:
    """
    1.0 with the whole hold ahead, 0.0 at expiry.

    A setup that has burned its expected duration without reaching its target
    was wrong about the speed of the move, which is usually the same thing as
    being wrong about the move.
    """
    if expires_at is None:
        return 0.5
    total = (expires_at - opened_at).total_seconds()
    if total <= 0:
        return 0.0
    left = (expires_at - now).total_seconds()
    return _clamp01(left / total)


def trend_confidence(pos, state, now: datetime) -> float:
    """
    The free half of the answer: is the reason this position exists still true?

    Weighted toward the two facts rather than the two indicators. Room before
    the stop and time remaining are measurements of this position; momentum
    and exhaustion are opinions about the market, and opinions have been wrong
    about this market at a measured rate of thirteen times in fourteen.
    """
    is_long = str(getattr(pos.side, "value", pos.side)).lower() == "long"
    price = state.current_price

    parts = (
        (room_before_stop(pos.entry_price, price, pos.stop_price, is_long), 0.35),
        (time_used(pos.opened_at, pos.expires_at, now), 0.25),
        (momentum_agrees(state, is_long), 0.25),
        (not_exhausted(state, is_long), 0.15),
    )
    return _clamp01(sum(score * weight for score, weight in parts))


def _needs_model(trend: float) -> bool:
    """
    Only when the model could change the answer.

    Below `HOLD_THRESHOLD - AI_MAX_HELP` the best case still closes; at or
    above `HOLD_THRESHOLD + AI_MAX_HARM` the worst case still holds. Deriving
    the band from the bounds rather than hardcoding it means loosening what
    the model may do cannot silently stop it being asked.
    """
    return (HOLD_THRESHOLD - AI_MAX_HELP) <= trend < (HOLD_THRESHOLD + AI_MAX_HARM)


def _no_answer(trend: float) -> Review:
    """
    The model was worth asking and did not answer.

    Closing rather than falling back to the local read, because the local read
    alone is exactly the thing that was judged insufficient here — that is why
    the model was being asked. Treating its silence as agreement would make an
    outage the most permissive state the system has.
    """
    from config.settings import settings

    if not settings.position_review_close_on_outage:
        return decide(trend)
    return Review(hold=False, confidence=trend, trend=trend,
                  reason="no model answer, closing on instruction")


def decide(trend: float, ai_delta: float | None = None,
           factors: str = "", summary: str = "") -> Review:
    """Combine the local read with the model's adjustment, if one was taken."""
    if ai_delta is None:
        hold = trend >= HOLD_THRESHOLD
        reason = ("clear enough locally" if not _needs_model(trend)
                  else "no model answer")
        return Review(hold=hold, confidence=trend, trend=trend, reason=reason)

    delta = max(-AI_MAX_HARM, min(AI_MAX_HELP, ai_delta))
    confidence = _clamp01(trend + delta)
    return Review(hold=confidence >= HOLD_THRESHOLD, confidence=confidence,
                  trend=trend, reason="local read plus model", asked_model=True,
                  factors=factors, summary=summary)


def trail_r_for_confidence(confidence: float, tight_r: float = 0.35,
                           loose_r: float = 1.10) -> float:
    """
    How far behind the high the stop rides, for a position in profit.

    The instruction was to trail by confidence rather than hold on faith, and
    the direction matters: MORE confidence buys a LOOSER trail, because the
    reason to give a trade room is that the reason for it still holds. Low
    confidence tightens toward the price and takes what is there.

    Expressed in R — multiples of the original stop distance — so it does not
    change meaning when leverage does. That distinction cost a silent bug
    once already: the margin-denominated form divided by leverage, and when
    leverage started moving with the stop the trail came out wider than the
    stop it was replacing and quietly never moved at all.
    """
    return tight_r + (loose_r - tight_r) * _clamp01(confidence)


# A closed vocabulary again, for the same reason the trade post-mortem has
# one: "it looked a bit weak" and "momentum_faded" mean the same thing, and
# only the second one can be counted. After a few hundred of these the
# question "which reason actually preceded a recovery" has an answer.
HOLD_FACTORS = [
    "trend_intact", "pullback_in_uptrend", "support_nearby", "volume_supports",
    "stop_still_far", "time_remaining", "momentum_faded", "trend_broke",
    "against_higher_timeframe", "no_buyers", "stop_imminent", "out_of_time",
]

REVIEW_SYSTEM = (
    "An open position is under review. Decide one thing: is the reason it was "
    "opened still true?\n\n"
    "That is the same question whether it is up or down, and your answer is "
    "used differently depending on which. A position in the red is closed "
    "unless the score clears the bar; a position in the green is not closed "
    "at all, its trailing stop is simply given more or less room. So do not "
    "reason about whether to take profit — reason about whether the move has "
    "further to go.\n\n"
    "You are adjusting a number, not making the call. A local reading of the "
    "market has already scored this; you can move that score by at most "
    f"+{AI_MAX_HELP:.2f} or -{AI_MAX_HARM:.2f}. Use the full range when you "
    "are sure and stay near zero when you are not.\n\n"
    "On a losing position the bias is toward closing. Cutting a loss early "
    "costs a spread; holding one that keeps going costs the trade. 'It might "
    "bounce' is true of every losing position ever opened and is not a "
    "reason.\n\n"
    "Judge only the numbers given. You have no news and no order book.\n\n"
    f"Tags, use only these: {', '.join(HOLD_FACTORS)}\n\n"
    "JSON only:\n"
    '{"reasoning": "what decides it, under 140 chars", '
    '"delta": -0.15 to 0.10, '
    '"factors": ["tag"], '
    '"summary": "one sentence as you would say it, under 120 chars"}'
)


def _describe(pos, state, trend: float, now: datetime) -> str:
    is_long = str(getattr(pos.side, "value", pos.side)).lower() == "long"
    price = state.current_price
    move = (price - pos.entry_price) / pos.entry_price * 100.0
    if not is_long:
        move = -move
    to_stop = abs(price - pos.stop_price) / price * 100.0
    held_min = (now - pos.opened_at).total_seconds() / 60.0
    left_min = ((pos.expires_at - now).total_seconds() / 60.0
                if pos.expires_at else None)
    return (
        f"{pos.symbol.upper()} {'LONG' if is_long else 'SHORT'} — "
        f"{pos.signal_type or 'setup'} at {round((pos.confidence or 0) * 100)}%\n"
        f"{'Up' if move >= 0 else 'Down'} {abs(move):.3f}% since entry "
        f"({'in profit — trail only, will not be closed on this' if move >= 0 else 'in the red'})\n"
        f"Stop is {to_stop:.3f}% away\n"
        f"Held {held_min:.0f} min"
        + (f", {left_min:.0f} min left before it expires\n" if left_min is not None else "\n")
        + f"RSI {state.rsi_14:.0f}, MACD histogram {getattr(state, 'macd_histogram', 0.0):+.4f}, "
          f"flow {getattr(state, 'cvd_trend', None) or 'n/a'}\n"
        f"Local read of all this: {trend:.2f} out of 1.00"
    )


async def review_position(pos, state, now: datetime, losing: bool = True) -> Review:
    """
    The full decision for a position at a loss: local read, then the model
    only if its answer could change the outcome.

    Never raises.

    When the model cannot be reached the position is CLOSED rather than held,
    on instruction. The two choices fail in opposite directions and neither is
    free: holding through an outage keeps a position a model might have shut,
    and closing books a real trade because of an API problem. The second was
    chosen because "keep the loss to a minimum" is the stated priority, and a
    closed trade at a small loss is recoverable in a way an open one running
    against you is not.

    This only applies to the band where the model was worth asking. Outside
    it the local read is decisive on its own and an outage changes nothing —
    a position at 0.95 is not closed because a provider timed out.
    """
    trend = trend_confidence(pos, state, now)

    # On a losing position the model is only worth asking inside the band
    # where its adjustment could cross the threshold. On a winning one there
    # is no threshold to cross — the score maps continuously onto how much
    # room the trail is given — so every point of it counts and the question
    # is always worth asking.
    if losing and not _needs_model(trend):
        return decide(trend)

    try:
        from collectors.llm_client import ask_json

        reply = await ask_json("position_review", REVIEW_SYSTEM,
                               _describe(pos, state, trend, now),
                               max_tokens=400, temperature=0.2, timeout=12.0)
    except Exception as exc:
        log.warning("position_review_failed", symbol=pos.symbol, error=str(exc)[:160])
        return _no_answer(trend)

    if not reply:
        return _no_answer(trend)

    from collectors.macro_sentinel import _clean_factors

    try:
        delta = float(reply.data.get("delta", 0.0) or 0.0)
    except (TypeError, ValueError):
        delta = 0.0

    review = decide(trend, delta,
                    factors=_clean_factors(reply.data.get("factors"), HOLD_FACTORS),
                    summary=str(reply.data.get("summary", "")).strip()[:200])
    log.info("position_reviewed", symbol=pos.symbol, verdict=review.verdict,
             trend=round(trend, 3), confidence=round(review.confidence, 3),
             factors=review.factors, served_by=reply.served_by)
    return review
