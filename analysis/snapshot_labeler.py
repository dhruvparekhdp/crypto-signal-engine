"""
Fill in what each snapshot was worth later.

The `crypto_snapshots` table has carried `price_30m_later`, `price_1h_later`,
`price_4h_later` and `price_1d_later` since the schema was written. Nothing has
ever populated them. Every row in the table has 0.0 in all four, which is the
column default — so the one table built to be a training set has features and
no labels, and has had none for as long as it has existed.

Nothing needs to be collected to fix that. A snapshot at 10:00 wants the price
at 10:30, and the snapshot taken at 10:30 already holds it. The label is a
self-join the writer never did.

Tolerance, not exactness
------------------------
Snapshots land every ~2 minutes, but on a wall clock, not a grid — a slow
collector round or a restart shifts them. Asking for the row at exactly T+30m
would find nothing most of the time, so this takes the nearest snapshot within
`TOLERANCE` and leaves the label unset when there is no row that close. A label
stitched from a price two hours off the horizon is worse than a missing one:
it is wrong, and nothing downstream can tell.

Why the label is a price, not a return
--------------------------------------
Because that is what the column says it is. The return is one subtraction away
and depends on choices — arithmetic or log, which price is the base — that
belong to whoever is doing the analysis, not to storage.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from storage.models import CryptoSnapshot

log = structlog.get_logger()

# Horizon column -> how far ahead it looks.
HORIZONS: dict[str, timedelta] = {
    "price_30m_later": timedelta(minutes=30),
    "price_1h_later": timedelta(hours=1),
    "price_4h_later": timedelta(hours=4),
    "price_1d_later": timedelta(days=1),
}

# Half the gap between the horizons closest together (30m and 1h) would be
# 15 minutes; 4 is tighter than it needs to be for a ~2-minute cadence and
# still absorbs a missed round or two.
TOLERANCE = timedelta(minutes=4)


@dataclass(frozen=True)
class LabelRun:
    """What one pass did, so the job can say something more than 'ok'."""

    scanned: int = 0
    labelled: int = 0
    unresolved: int = 0

    @property
    def coverage(self) -> float:
        done = self.labelled + self.unresolved
        return self.labelled / done if done else 0.0


def _nearest(times: list[datetime], target: datetime) -> int | None:
    """Index of the timestamp closest to `target`, or None if none is close."""
    i = bisect.bisect_left(times, target)
    best: int | None = None
    for k in (i - 1, i):
        if 0 <= k < len(times) and abs(times[k] - target) <= TOLERANCE:
            if best is None or abs(times[k] - target) < abs(times[best] - target):
                best = k
    return best


def _as_naive(moment: datetime) -> datetime:
    """
    SQLite hands back naive datetimes, Postgres aware ones, and comparing the
    two raises. Everything here is UTC, so drop the tzinfo and compare plainly
    rather than making every caller remember which backend it is on.
    """
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


def label_rows(rows: list[CryptoSnapshot], now: datetime) -> LabelRun:
    """
    Fill the forward-price columns in place. Pure, so it can be tested without
    a database: the caller loads the rows and commits whatever this changed.

    `rows` may hold several symbols — they are separated here, because BTC's
    price half an hour after an ETH snapshot is not a label, it is a bug.
    """
    by_symbol: dict[str, list[CryptoSnapshot]] = {}
    for row in rows:
        by_symbol.setdefault(row.symbol, []).append(row)

    now = _as_naive(now)
    labelled = unresolved = 0

    for series in by_symbol.values():
        series.sort(key=lambda r: _as_naive(r.timestamp))
        times = [_as_naive(r.timestamp) for r in series]

        for i, row in enumerate(series):
            for column, horizon in HORIZONS.items():
                if getattr(row, column):
                    continue  # already labelled; a real price is never 0.0
                if times[i] + horizon > now:
                    continue  # the future has not happened yet, so no label is due

                found = _nearest(times, times[i] + horizon)
                if found is None or not series[found].price:
                    unresolved += 1
                    continue
                setattr(row, column, series[found].price)
                labelled += 1

    return LabelRun(scanned=len(rows), labelled=labelled, unresolved=unresolved)


async def backfill_labels(session, lookback_days: int = 30) -> LabelRun:
    """
    Label every snapshot in the window that is old enough to have a label.

    Bounded by `lookback_days` rather than scanning the table: rows whose
    forward snapshot never arrived — a restart, an outage — can never be
    filled, and without a bound the job would re-attempt them for the life of
    the database. Within the window the retry is cheap and occasionally
    succeeds, because a gap can be filled later by a backfilled row.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=lookback_days)

    result = await session.execute(
        select(CryptoSnapshot).where(CryptoSnapshot.timestamp >= cutoff)
    )
    rows = list(result.scalars().all())

    run = label_rows(rows, now)
    if run.labelled:
        await session.commit()

    log.info("snapshot_labels_written", scanned=run.scanned, labelled=run.labelled,
             unresolved=run.unresolved, coverage=round(run.coverage, 4))
    return run
