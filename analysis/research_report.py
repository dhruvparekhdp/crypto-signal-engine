"""
What the history actually says, before anyone asks a model about it.

The temptation with months of market data and four API keys is to hand the
rows to a model and ask what it sees. That does not work, for a reason worth
stating plainly: 35,000 snapshots is roughly 1.5M tokens, which is expensive
to send and — far worse — no model computes a rank correlation over 35,000
rows in its head. Asked to, it produces a number with the shape of an answer.

So the arithmetic happens here, in code that can be checked, and the model is
given the RESULT. The report below is a couple of kilobytes. That inverts the
economics: the analysis is free and exact, and the expensive model is spent
on the part it is actually good at — reading a table of weak effects and
proposing which one is worth a week of testing.

What it measures
----------------
Information coefficient, the rank correlation between a feature now and the
return later. It is the standard first question of quantitative research and
the honest one: |IC| under 0.02 is noise, 0.03-0.05 is a weak real effect,
and anything above 0.1 on market data usually means a bug rather than an edge.

Then the same features in deciles, because an IC is one number over a
monotonic assumption and the deciles show whether the effect is real at the
extremes or an artefact of the middle.

And every figure is placed against the round-trip cost for that instrument,
because a signal that predicts a move smaller than its own fees is not a
weak edge. It is a losing trade with good statistics.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from analysis.instruments import spec_for
from storage.models import CryptoSnapshot

log = structlog.get_logger()

# The label columns, shortest first, with the name a human would use.
_HORIZON_LABELS = [("price_30m_later", "30m"), ("price_1h_later", "1h"),
                   ("price_4h_later", "4h"), ("price_1d_later", "1d")]


def _bollinger_position(row) -> float | None:
    span = row.bollinger_upper - row.bollinger_lower
    return (row.price - row.bollinger_lower) / span if span > 0 else None


FEATURES: dict[str, object] = {
    "rsi_14": lambda r: r.rsi_14,
    "macd_histogram": lambda r: r.macd_line - r.macd_signal,
    "bollinger_position": _bollinger_position,
    "atr_pct": lambda r: (r.atr_14 / r.price * 100.0) if r.price else None,
    "volume_24h": lambda r: r.volume_24h,
}


def spearman(xs: list[float], ys: list[float]) -> float:
    """
    Rank correlation. Rank rather than Pearson because a single bad print —
    and this data has had them — moves a Pearson correlation a long way and a
    rank correlation by one position.
    """
    if len(xs) < 30:
        return 0.0

    def ranked(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for position, index in enumerate(order):
            out[index] = float(position)
        return out

    rx, ry = ranked(xs), ranked(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


@dataclass
class Findings:
    rows: int = 0
    rejected: int = 0
    symbols: list[str] = field(default_factory=list)
    span_days: float = 0.0
    cost_pct: float = 0.0
    baseline: dict[str, tuple[float, float]] = field(default_factory=dict)
    ic: dict[str, dict[str, float]] = field(default_factory=dict)
    deciles: dict[str, list[tuple[float, float, int, float]]] = field(default_factory=dict)
    clears_cost: dict[str, float] = field(default_factory=dict)


def _forward_returns(row) -> dict[str, float]:
    """Percentage move to each horizon, skipping labels that were never filled."""
    out = {}
    for column, name in _HORIZON_LABELS:
        later = getattr(row, column, 0.0)
        if later and row.price:
            out[name] = (later - row.price) / row.price * 100.0
    return out


# A price this far from the symbol's own median for the window is not a move,
# it is a bad print. Five-fold is deliberately loose — nothing in this
# watchlist has ever moved that much in a week, and the prints being caught
# are off by eight orders of magnitude, not by a factor of six.
_IMPLAUSIBLE_RATIO = 5.0

# And a bound on the label itself, for the case the filter above misses: a
# base price that survives against a forward price that did not.
_IMPLAUSIBLE_RETURN_PCT = 50.0


def _plausible_price_band(rows: list) -> dict[str, tuple[float, float]]:
    """
    The range each symbol's price is allowed to be in, from its own median.

    Gold spent 55 snapshots priced at 4.3e-05 instead of 4350 — off by eight
    orders of magnitude, from a feed handing back the wrong unit. Those rows
    pass a `price > 0` check, which is why the collector's guard let them
    through, and one of them in a denominator turns a 0.04% move into a
    forward return of 15 million percent. That number then lands in a mean
    and takes the whole report with it.

    Measured against the median rather than the mean for the obvious reason:
    a mean computed over data containing 4.3e-05 is not a reference point.
    """
    prices: dict[str, list[float]] = {}
    for row in rows:
        if row.price and row.price > 0:
            prices.setdefault(row.symbol, []).append(row.price)
    band = {}
    for sym, values in prices.items():
        middle = st.median(values)
        band[sym] = (middle / _IMPLAUSIBLE_RATIO, middle * _IMPLAUSIBLE_RATIO)
    return band


def analyse(rows: list, symbol: str | None = None) -> Findings:
    """
    Measure. Pure, so the arithmetic can be tested without a database and
    without a model — which is the point of doing it here rather than asking.

    Bad prints are dropped before anything is computed. That is not tidying:
    a report built over them is not merely noisy, it is confidently wrong,
    and it would be handed to a model that has no way to tell.
    """
    band = _plausible_price_band(rows)
    usable = []
    rejected = 0
    for row in rows:
        low, high = band.get(row.symbol, (0.0, float("inf")))
        if not row.price or not (low <= row.price <= high):
            rejected += 1
            continue
        forwards = {n: v for n, v in _forward_returns(row).items()
                    if abs(v) <= _IMPLAUSIBLE_RETURN_PCT}
        if forwards:
            usable.append((row, forwards))
    if rejected:
        log.warning("research_dropped_implausible_prices", rejected=rejected,
                    kept=len(usable))

    found = Findings(rows=len(usable), rejected=rejected)
    if not usable:
        return found

    found.symbols = sorted({r.symbol for r, _ in usable})
    stamps = [r.timestamp for r, _ in usable]
    found.span_days = round((max(stamps) - min(stamps)).total_seconds() / 86400, 1)
    found.cost_pct = spec_for(symbol or found.symbols[0]).round_trip_pct * 100.0

    for _, name in _HORIZON_LABELS:
        series = [f[name] for _, f in usable if name in f]
        if len(series) < 30:
            continue
        found.baseline[name] = (st.mean(series), st.pstdev(series))
        found.clears_cost[name] = (
            sum(1 for v in series if abs(v) > found.cost_pct) / len(series) * 100.0)

    for feature, extract in FEATURES.items():
        found.ic[feature] = {}
        for _, name in _HORIZON_LABELS:
            pairs = [(extract(r), f[name]) for r, f in usable
                     if name in f and extract(r) is not None]
            if len(pairs) < 30:
                continue
            found.ic[feature][name] = spearman([a for a, _ in pairs],
                                               [b for _, b in pairs])

    # Deciles on the 1h horizon: long enough to clear the noise of a couple of
    # ticks, short enough that a scalping system might actually hold for it.
    for feature, extract in FEATURES.items():
        pairs = sorted(((extract(r), f["1h"]) for r, f in usable
                        if "1h" in f and extract(r) is not None), key=lambda p: p[0])
        if len(pairs) < 300:
            continue
        size = len(pairs) // 10
        buckets = []
        for i in range(10):
            chunk = pairs[i * size:(i + 1) * size if i < 9 else len(pairs)]
            returns = [b for _, b in chunk]
            buckets.append((chunk[0][0], chunk[-1][0], len(chunk), st.mean(returns)))
        found.deciles[feature] = buckets

    return found


def render(found: Findings) -> str:
    """
    The report, as plain text, sized to be read by a person or sent to a
    model. A couple of kilobytes rather than a couple of megabytes.
    """
    if not found.rows:
        return ("No labelled snapshots yet. Labels are written 30 minutes to a day "
                "after each snapshot, so a freshly started engine has none.")

    out = [
        f"{found.rows:,} labelled snapshots across {len(found.symbols)} symbols, "
        f"{found.span_days} days ({', '.join(found.symbols)})"
        + (f" — {found.rejected:,} rows dropped as bad prints" if found.rejected else ""),
        f"Round-trip cost: {found.cost_pct:.4f}% — every figure below is worth "
        f"reading against this number.",
        "",
        "BASELINE — what a random entry gets",
        f"{'horizon':<10}{'mean %':>10}{'sd %':>10}{'|move| > cost':>16}",
    ]
    for _, name in _HORIZON_LABELS:
        if name not in found.baseline:
            continue
        mean, sd = found.baseline[name]
        out.append(f"{name:<10}{mean:>+10.4f}{sd:>10.4f}"
                   f"{found.clears_cost.get(name, 0.0):>15.1f}%")

    horizons = [n for _, n in _HORIZON_LABELS if n in found.baseline]
    out += ["", "INFORMATION COEFFICIENT — rank correlation, feature now vs return later",
            "|IC| < 0.02 is noise · 0.03-0.05 is a weak real effect · > 0.1 is usually a bug",
            f"{'feature':<20}" + "".join(f"{h:>10}" for h in horizons)]
    for feature, per_horizon in found.ic.items():
        if not per_horizon:
            continue
        line = f"{feature:<20}"
        for horizon in horizons:
            value = per_horizon.get(horizon)
            line += f"{value:>+10.4f}" if value is not None else f"{'—':>10}"
        out.append(line)

    for feature, buckets in found.deciles.items():
        strongest = abs(found.ic.get(feature, {}).get("1h", 0.0))
        if strongest < 0.02:
            continue  # a decile table for a feature that predicts nothing is noise
        out += ["", f"DECILES — {feature} vs forward 1h return",
                f"{'bucket':<22}{'n':>7}{'mean %':>10}{'vs cost':>10}"]
        for low, high, count, mean in buckets:
            verdict = "clears" if abs(mean) > found.cost_pct else "under"
            out.append(f"{low:>9.3f} - {high:<9.3f}{count:>7}{mean:>+10.4f}{verdict:>10}")

    return "\n".join(out)


async def load_and_analyse(session, days: int = 30, symbol: str | None = None) -> Findings:
    """Read the labelled window out of the database and measure it."""
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    query = select(CryptoSnapshot).where(CryptoSnapshot.timestamp >= cutoff)
    if symbol:
        query = query.where(CryptoSnapshot.symbol == symbol.lower())
    rows = list((await session.execute(query)).scalars().all())
    return analyse(rows, symbol)


RESEARCH_SYSTEM = (
    "You are looking at measured statistics from a live crypto scalping "
    "system — not at raw prices, and not at a backtest. Somebody computed "
    "these from the system's own history.\n\n"
    "The one thing that matters: every predicted move has to clear the "
    "round-trip cost stated at the top. A feature with a real information "
    "coefficient that moves price less than its own fees is not a weak edge, "
    "it is a losing trade with good statistics. Say so when you see it.\n\n"
    "Read the signs. If the correlations are negative the market is "
    "mean-reverting at these horizons, and a system trading them as momentum "
    "is backwards — that is worth more than any new indicator.\n\n"
    "Do not invent numbers. You have exactly what is below: no news, no "
    "order book, no macro. If the answer is that nothing here pays for "
    "itself yet, say that; it is more useful than a plausible suggestion.\n\n"
    "Propose things this system can actually test with what it already "
    "collects. Each one needs a number from the report behind it.\n\n"
    "JSON only:\n"
    '{"reading": "what the numbers say, 2-3 sentences", '
    '"biggest_problem": "the single thing most limiting returns, one sentence", '
    '"proposals": [{"change": "what to try, one sentence", '
    '"because": "which figure above motivates it", '
    '"test": "how you would know within two weeks whether it worked"}], '
    '"not_worth_trying": ["things the data argues against, so they stay dropped"]}'
)


async def ask_for_hypotheses(found: Findings) -> dict:
    """
    Hand the measurements to the research chain and get back things to test.

    Never raises and never blocks anything: this runs weekly, off the trading
    path entirely, and an unavailable model costs the report's commentary and
    not the report.
    """
    from collectors.llm_client import ask_json

    if not found.rows:
        return {}

    reply = await ask_json("research", RESEARCH_SYSTEM, render(found),
                           max_tokens=2000, temperature=0.4, timeout=120.0)
    if not reply:
        return {}
    out = dict(reply.data)
    out["served_by"] = reply.served_by
    log.info("research_pass_complete", rows=found.rows, served_by=reply.served_by,
             proposals=len(out.get("proposals") or []))
    return out
