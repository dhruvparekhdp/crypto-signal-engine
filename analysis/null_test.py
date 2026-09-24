"""
Does the signal set beat entering at random?

Every other measurement in this repo asks how a strategy performed. This asks
the only question that decides whether it is a strategy: replay the same
number of entries at random times, on random symbols, in random directions,
through the same stop, target and holding period — and see whether the real
signals did better.

Why this and not a win rate
---------------------------
A win rate is not comparable to anything. 24% sounds poor and 48% sounds fine,
but both are meaningless without knowing what the market handed out for free
that week. In a strong trend a coin flip wins often; in a chop it wins rarely.
Run on the first restored week of live data, the signals returned -25.5% at
the settings in production while random entries over the same bars returned
-18.3% — so the detectors were not weak, they were worse than nothing, and no
win rate would have said so.

The comparison holds the market, the period, the instruments and the exit
rules constant. The only thing it varies is WHEN and WHAT you enter, which is
precisely and only what a detector claims to know.

Reading the result
------------------
`p_value` is the share of random trials that matched or beat the real signals.
Above ~0.5 the detectors are doing nothing. Below 0.05 there is something
worth keeping, on this sample. It is a permutation test rather than a
parametric one because trade returns are nowhere near normal — a handful of
trailing winners carry the whole distribution, and a t-test on that lies.

This is a measurement, not a verdict. One week of one regime can fail to
detect an edge that exists; it cannot prove one does not.
"""
from __future__ import annotations

import bisect
import random
import statistics as st
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class Entry:
    symbol: str
    direction: str
    at: datetime


@dataclass(frozen=True)
class NullResult:
    entries: int
    real_total: float
    random_mean: float
    random_sd: float
    trials: int
    beaten_by: int

    @property
    def p_value(self) -> float:
        """Share of random trials that matched or beat the real signals."""
        return self.beaten_by / self.trials if self.trials else 1.0

    @property
    def edge(self) -> float:
        """How much the real signals added over random, in total percent."""
        return self.real_total - self.random_mean

    @property
    def verdict(self) -> str:
        if self.p_value <= 0.05:
            return "beats random"
        if self.p_value >= 0.50:
            return "no better than random"
        return "inconclusive"


def replay(entries: list[Entry], paths: dict[str, tuple[list[datetime], list[float]]],
           stop_pct: float, target_pct: float, hold_hours: float,
           cost_pct: float) -> tuple[int, int, float]:
    """
    Walk each entry forward bar by bar until stop, target or the clock.

    Bar by bar rather than close to close because which level is reached
    FIRST is the entire question, and a close-to-close reading cannot see a
    stop that was touched and recovered inside the hour.
    """
    taken = wins = 0
    total = 0.0
    for entry in entries:
        times, prices = paths.get(entry.symbol, ([], []))
        if not times:
            continue
        i = bisect.bisect_left(times, entry.at)
        if i >= len(times):
            continue

        start = prices[i]
        sign = 1.0 if entry.direction == "long" else -1.0
        target = start * (1 + sign * target_pct / 100)
        stop = start * (1 - sign * stop_pct / 100)
        deadline = entry.at + timedelta(hours=hold_hours)

        outcome = None
        j = i
        for j in range(i, len(times)):
            if times[j] > deadline:
                break
            price = prices[j]
            if sign * (price - stop) <= 0:
                outcome = -stop_pct
                break
            if sign * (price - target) >= 0:
                outcome = target_pct
                wins += 1
                break
        if outcome is None:                       # ran out of clock or of data
            outcome = sign * (prices[min(j, len(prices) - 1)] - start) / start * 100

        total += outcome - cost_pct
        taken += 1
    return taken, wins, total


def compare_against_random(real: list[Entry],
                           paths: dict[str, tuple[list[datetime], list[float]]],
                           stop_pct: float, target_pct: float, hold_hours: float,
                           cost_pct: float, trials: int = 200,
                           seed: int = 7) -> NullResult:
    """
    Replay the real entries, then `trials` sets of random ones the same size.

    Random entries are drawn only from timestamps with a full holding period
    of data after them. Without that the random set is quietly truncated near
    the end of the sample and scores differently for a reason that has nothing
    to do with entry quality.
    """
    taken, _, real_total = replay(real, paths, stop_pct, target_pct, hold_hours, cost_pct)
    if not taken:
        return NullResult(0, 0.0, 0.0, 0.0, 0, 0)

    every = sorted({t for times, _ in paths.values() for t in times})
    if not every:
        return NullResult(taken, real_total, 0.0, 0.0, 0, 0)
    latest = every[-1] - timedelta(hours=hold_hours)
    candidates = [t for t in every if t <= latest]
    symbols = sorted(paths)
    if not candidates or not symbols:
        return NullResult(taken, real_total, 0.0, 0.0, 0, 0)

    rng = random.Random(seed)
    totals = []
    for _ in range(trials):
        fake = [Entry(rng.choice(symbols), rng.choice(("long", "short")),
                      rng.choice(candidates)) for _ in range(len(real))]
        totals.append(replay(fake, paths, stop_pct, target_pct, hold_hours, cost_pct)[2])

    beaten = sum(1 for t in totals if t >= real_total)
    result = NullResult(entries=taken, real_total=real_total,
                        random_mean=st.mean(totals), random_sd=st.pstdev(totals),
                        trials=trials, beaten_by=beaten)
    log.info("null_test", entries=taken, real=round(real_total, 2),
             random=round(result.random_mean, 2), p=round(result.p_value, 3),
             verdict=result.verdict)
    return result


def render(result: NullResult, stop_pct: float, target_pct: float,
           hold_hours: float) -> str:
    if not result.entries:
        return "Not enough data to replay — no entries landed inside the price history."
    return "\n".join([
        f"Null test — stop {stop_pct}% / target {target_pct}% / {hold_hours}h hold",
        f"  entries replayed        {result.entries}",
        f"  real signals            {result.real_total:+.1f}%",
        f"  random entries          {result.random_mean:+.1f}%  "
        f"(sd {result.random_sd:.1f}, {result.trials} trials)",
        f"  difference              {result.edge:+.1f}%",
        f"  random matched or beat  {result.beaten_by}/{result.trials}"
        f"  ->  p = {result.p_value:.2f}",
        f"  verdict                 {result.verdict.upper()}",
    ])
