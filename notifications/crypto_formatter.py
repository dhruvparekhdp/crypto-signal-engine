from __future__ import annotations

from analysis.crypto_signal import CryptoSignal
from analysis.scalp_levels import ScalpConfig, trailing_plan
from config.settings import settings

# Plain-English headline for each signal_type — the underlying jargon
# (RSI divergence, Bollinger squeeze, etc.) is still explained in the
# glossary on the dashboard's Crypto tab, not repeated in every alert.
SIGNAL_NAME = {
    "rsi_divergence": "Momentum Reversal",
    "volume_spike": "Volume Surge",
    "bollinger_squeeze": "Breakout Setup",
    "sentiment_shift": "News Catalyst",
}


def format_crypto_signal(sig: CryptoSignal) -> str:
    """Format a CryptoSignal dataclass into a short, plain-English Telegram message."""
    dir_emoji = "🟢 LONG" if sig.direction == "long" else "🔴 SHORT"
    symbol_display = sig.symbol.upper()
    name = SIGNAL_NAME.get(sig.signal_type, sig.signal_type.replace("_", " ").title())

    conf_pct = int(sig.confidence * 100)
    target_str = f"${sig.target_price:,.4f}" if sig.target_price else "n/a"
    stop_str = f"${sig.stop_loss:,.4f}" if sig.stop_loss else "n/a"
    move_pct = (abs(sig.target_price - sig.current_price) / sig.current_price * 100
                if sig.target_price and sig.current_price else 0.0)
    # The full round trip for THIS market, from the same config the engine
    # sizes against. This was brokerage alone, hardcoded in this file — so a
    # Telegram alert claimed 7.2x on a target the engine scored at 5.0x, and
    # gold was costed as though it paid ether's brokerage.
    round_trip = ScalpConfig().for_symbol(sig.symbol).cost_floor_pct * 100
    x_cost = move_pct / round_trip if round_trip else 0.0
    risk_pct = (abs(sig.current_price - sig.stop_loss) / sig.current_price * 100
                if sig.stop_loss and sig.current_price else 0.0)
    rr = move_pct / risk_pct if risk_pct else 0.0
    stake_amt = round(sig.stake_pct * settings.bank_size, 2)
    stake_pct_display = round(sig.stake_pct * 100, 2)

    # The trail, in prices, because the venue's TP/SL box takes prices and this
    # has to be followable by hand. Without these two lines the alert publishes
    # a 2R target and says nothing about the exit that actually pays for it.
    plan = trailing_plan(sig.current_price, sig.stop_loss,
                         ScalpConfig().for_symbol(sig.symbol))
    trail_lines = ""
    if plan:
        trail_lines = (
            f"\n<b>Trail</b> at <b>${plan.arm_price:,.4f}</b> "
            f"→ stop to <b>${plan.breakeven_stop:,.4f}</b> (breakeven), "
            f"then ride <b>${plan.trail_distance:,.4f}</b> behind the high\n"
        )

    ai_review_block = ""
    if getattr(sig, "ai_review", ""):
        ai_review_block = f"\n\n🤖 <b>AI Review (Groq Sentinel):</b>\n<i>\"{sig.ai_review}\"</i>"

    return (
        f"🪙 <b>{symbol_display}</b> · {dir_emoji}\n"
        # The timeframe is now the window the move is expected to need, and
        # it is spelled out rather than left as a bare "15m" — the point of
        # running two horizons is being able to compare them by eye.
        f"<b>{name}</b> · expected to take about <b>{sig.timeframe}</b>\n\n"
        f"{sig.trigger_description}\n\n"
        f"Entry <b>${sig.current_price:,.4f}</b> → "
        f"Target <b>{target_str}</b> → Stop <b>{stop_str}</b>\n"
        f"Move <b>{move_pct:.3f}%</b> · <b>{x_cost:.1f}×</b> what the round trip costs · "
        f"reward:risk <b>{rr:.2f}</b>\n"
        + trail_lines +
        f"Confidence <b>{conf_pct}%</b> · Suggested stake <b>₹{stake_amt:,.0f}</b> "
        f"({stake_pct_display}% of bank)"
        + ai_review_block
    )


_REASON_TEXT = {
    "target": "hit target",
    "stop": "stopped out",
    "profit_lock": "profit locked in",
    "liquidation": "LIQUIDATED",
    "expiry": "time expired",
    "cycle_end": "cycle closed",
    "signal_flip": "setup reversed",
    "conviction_lost": "conviction faded",
    "market_shock": "market shock",
}


def format_paper_trade(trade, wallet: float) -> str:
    """
    One closed paper trade, with the costs shown rather than netted away.

    Gross and fees are both printed because a run of small "wins" that are net
    losses is the exact failure this system was built to catch, and a message
    showing only the net number hides it.
    """
    pos = trade.position
    won = trade.net_pnl > 0
    icon = "\u2705" if won else "\u274c"
    arrow = "LONG" if pos.side.value == "long" else "SHORT"
    reason = _REASON_TEXT.get(trade.reason.value, trade.reason.value)
    fees = trade.fees_paid - trade.funding_paid

    lines = [
        f"{icon} <b>{pos.symbol.upper()}</b> {arrow} — {reason}",
        f"Entry <b>{pos.entry_price:,.4f}</b> \u2192 Exit <b>{trade.exit_price:,.4f}</b>",
        f"Gross <b>\u20b9{trade.gross_pnl:,.2f}</b> "
        f"\u2212 fees \u20b9{fees:,.2f}"
        + (f" \u2212 funding \u20b9{trade.funding_paid:,.2f}" if trade.funding_paid else ""),
        f"<b>Net \u20b9{trade.net_pnl:+,.2f}</b> "
        f"({trade.return_on_margin * 100:+.1f}% on \u20b9{pos.margin:,.0f} margin)",
        f"Wallet <b>\u20b9{wallet:,.2f}</b>",
    ]
    return "\n".join(lines)


def format_cycle_end(cycle, outcome: str, summary: dict) -> str:
    """The scorecard when a cycle finishes, so the next one can be judged against it."""
    headline = {
        "hit_target": "\U0001f3af Target reached",
        "busted": "\U0001f480 Wallet exhausted",
        "stopped": "\u23f9 Cycle stopped",
    }.get(outcome, outcome)

    rr = summary.get("realised_reward_risk")
    costs = summary.get("costs_as_pct_of_gross")
    lines = [
        f"<b>{headline}</b> — cycle #{cycle.id}",
        f"Wallet \u20b9{cycle.starting_wallet:,.0f} \u2192 <b>\u20b9{summary['wallet']:,.2f}</b>",
        f"{summary['trades']} trades, {summary['win_rate_pct']}% won",
        f"Gross \u20b9{summary['gross_pnl']:,.2f} \u2212 costs "
        f"\u20b9{summary['trading_fees'] + summary['funding_paid']:,.2f} "
        f"= <b>\u20b9{summary['net_pnl']:+,.2f}</b>",
    ]
    if costs is not None:
        lines.append(f"Costs took {costs}% of gross profit")
    if rr:
        lines.append(f"Realised reward:risk {rr}")
    lines.append(f"Longest losing streak {summary['longest_losing_streak']}")
    return "\n".join(lines)
