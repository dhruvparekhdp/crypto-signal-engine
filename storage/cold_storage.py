"""
The database keeps one year. Everything older lives in JSON files.

The owner's rule: never delete history, but do not keep more than a year of
it in the database. So rows older than `db_retention_days` (365) are written
to gzipped JSON lines, one file per table per month:

    data/archive/{table}/{YYYY-MM}.jsonl.gz

and only then removed from the database. The files are plain JSON — any
tool, any language, `zcat | jq` — and `read_rows` streams them back for
analysis. Copy the folder off the server with the database backups.

Safety:
  * Rows are taken in id order, in batches, below an id ceiling read at the
    start, so rows written while this runs are never touched.
  * Each batch is written and flushed to disk BEFORE its rows are deleted.
    If the process dies between the two, the next run writes those rows
    again; every record carries its `id`, and `read_rows` drops repeats.
  * A table whose time column is missing, or a dry run, changes nothing.

Tables with configuration or open state (watchlist, open positions, settings,
auth) are not listed and are never touched.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

# table -> the column that says how old a row is
TIME_COLUMN = {
    "market_candles": "open_time",
    "crypto_snapshots": "timestamp",
    "crypto_snapshots_archive": "timestamp",
    "commodity_snapshots": "timestamp",
    "commodity_snapshots_archive": "timestamp",
    "crypto_signal_log": "timestamp",
    "signal_reviews": "created_at",
    "news_sentiment": "received_at",
    "market_briefings": "created_at",
    "move_attributions": "created_at",
    "market_events": "last_seen",
    "event_shadow_trades": "created_at",
    "paper_trades": "closed_at",
    "v2_shadow_signals": "decided_at",
}


def _model(table: str):
    from storage.models import Base
    for mapper in Base.registry.mappers:
        if mapper.class_.__tablename__ == table:
            return mapper.class_
    return None


def _plain(v):
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


def _row_dict(obj) -> dict:
    """Column name -> JSON-safe value, via the mapper so attribute names may differ."""
    from sqlalchemy import inspect
    return {attr.columns[0].name: _plain(getattr(obj, attr.key))
            for attr in inspect(obj).mapper.column_attrs}


def file_for(root: Path, table: str, when: datetime) -> Path:
    return Path(root) / table / f"{when:%Y-%m}.jsonl.gz"


def append_rows(root: Path, table: str, rows: list[dict], time_col: str) -> int:
    """
    Write rows to NEW files, one per month in the batch, and make them durable.
    Returns rows written.

    Each batch gets its own file ({YYYY-MM}.{first}-{last}.jsonl.gz), written
    under a temporary name, fsynced, then renamed. Appending gzip members to
    one month file (the first version) meant a crash mid-write left a broken
    member that stopped every reader before the rows written after it — rows
    already deleted from the database.
    """
    by_month: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        when = datetime.fromisoformat(r[time_col]) if r.get(time_col) else datetime(1970, 1, 1)
        by_month[f"{when:%Y-%m}"].append(r)
    folder = Path(root) / table
    folder.mkdir(parents=True, exist_ok=True)
    for month, items in by_month.items():
        ids = [r.get("id", 0) or 0 for r in items]
        # Unique per write: a re-run after a crash writes the same ids again
        # and must add a file, never replace one.
        path = folder / f"{month}.{min(ids)}-{max(ids)}.{time.time_ns()}.jsonl.gz"
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                for r in items:
                    gz.write((json.dumps(r, separators=(",", ":")) + "\n").encode())
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, path)
    try:
        dir_fd = os.open(folder, os.O_RDONLY)
        os.fsync(dir_fd)                   # the rename itself is durable too
        os.close(dir_fd)
    except OSError:
        pass
    return len(rows)


def read_rows(root: Path, table: str, start: datetime | None = None,
              end: datetime | None = None) -> Iterator[dict]:
    """Stream a table's archived rows back, oldest month first, repeats dropped."""
    folder = Path(root) / table
    if not folder.exists():
        return
    time_col = TIME_COLUMN.get(table)
    # (id, time) rather than id alone: SQLite reuses ids once a table has
    # been emptied, and two different rows must not collapse into one.
    seen: set = set()
    for path in sorted(folder.glob("*.jsonl.gz")):
        month = path.name[:7]
        if start and month < f"{start:%Y-%m}":
            continue
        if end and month > f"{end:%Y-%m}":
            continue
        with gzip.open(path, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                key = ((r.get("id"), r.get(time_col)) if time_col
                       else json.dumps(r, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                if time_col and r.get(time_col):
                    t = datetime.fromisoformat(r[time_col])
                    if (start and t < start) or (end and t >= end):
                        continue
                yield r


async def offload(session, root: Path, days: int = 365, batch: int = 5000,
                  dry_run: bool = False, tables: list[str] | None = None) -> dict[str, int]:
    """
    Move every row older than `days` from the listed tables to JSON files.
    Returns {table: rows moved} (or rows that would move, on a dry run).
    """
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    moved: dict[str, int] = {}
    for table in tables or list(TIME_COLUMN):
        model = _model(table)
        col_name = TIME_COLUMN.get(table)
        if model is None or col_name is None or col_name not in model.__table__.c:
            continue
        col = getattr(model, col_name)
        if dry_run:
            n = (await session.execute(
                select(func.count()).select_from(model).where(col < cutoff))).scalar() or 0
            moved[table] = int(n)
            continue
        ceiling = (await session.execute(select(func.max(model.id)))).scalar()
        if ceiling is None:
            moved[table] = 0
            continue
        total, last_id = 0, 0
        while True:
            rows = (await session.execute(
                select(model).where(col < cutoff, model.id <= ceiling, model.id > last_id)
                .order_by(model.id).limit(batch))).scalars().all()
            if not rows:
                break
            data = [_row_dict(r) for r in rows]
            ids = [r.id for r in rows]
            last_id = ids[-1]
            append_rows(root, table, data, col_name)
            await session.execute(delete(model).where(model.id.in_(ids))
                                  .execution_options(synchronize_session=False))
            await session.commit()
            session.expunge_all()
            total += len(ids)
        moved[table] = total
    return moved
