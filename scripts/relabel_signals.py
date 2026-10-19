"""Re-label past intraday signals with the resolver's rules (owner, 19 Oct 2026: "yes go ahead, fix the resolver
labels"). analysis/signal_resolution.py decides; this only applies it to the signals logged before the fix.

    python -m scripts.relabel_signals compute SIGNALS.json.gz OUT.json    # local: 1m bars from the lake (+ REST top-up)
    python -m scripts.relabel_signals apply OUT.json                      # server: back up, then update changed rows

compute: every non-swing signal whose window has fully passed gets the label the new rules give on Binance 1m bars
(the lake that scripts/signal_replay.py loads). apply: writes a backup of the whole signal log to ~/backups first, then
updates outcome and pnl_pct only where the label changed AND the row still holds the label we computed from (so a row
the live job touched meanwhile is left alone). Nothing is deleted.
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def compute(signals_path: str, out_path: str) -> None:
    import pandas as pd

    from analysis.signal_resolution import resolve, window_minutes
    from scripts.signal_replay import LAKE, SYMBOL_MAP, load_1m
    sigs = json.load(gzip.open(signals_path) if signals_path.endswith(".gz") else open(signals_path))
    sigs = [s for s in sigs if s.get("trade_mode") != "swing" and not s.get("suppressed_by")]
    since = min(s["timestamp"] for s in sigs)[:10]
    frames: dict[str, pd.DataFrame] = {}
    changes, same, skipped = [], 0, {}
    for s in sigs:
        src = SYMBOL_MAP.get(s["symbol"].upper(), s["symbol"].upper())
        if src not in frames:
            frames[src] = load_1m(src, since, Path("data/lab/signal_replay/bars")) if (LAKE / "klines" / src).exists() \
                else pd.DataFrame()
        df = frames[src]
        if df.empty:
            skipped[src] = skipped.get(src, 0) + 1
            continue
        t0 = pd.Timestamp(s["timestamp"], tz="UTC")
        w = window_minutes(s.get("timeframe"))
        a = int(t0.timestamp() * 1000) - 120_000
        seg = df[(df.t >= a) & (df.t <= a + (w + 5) * 60_000)]
        bars = [{"timestamp": datetime.fromtimestamp(t / 1000, tz=UTC), "open": o, "high": h, "low": lo, "close": c}
                for t, o, h, lo, c in zip(seg.t, seg.open, seg.high, seg.low, seg.close)]
        if s["current_price"] <= 0.001 or s["target_price"] <= 0 or s["stop_loss"] <= 0:
            continue
        r = resolve(s["direction"], s["current_price"], s["target_price"], s["stop_loss"],
                    datetime.fromisoformat(s["timestamp"]), bars, w)
        if r.status in ("running", "gap"):
            skipped[r.status] = skipped.get(r.status, 0) + 1
            continue
        if r.status == s["outcome"] and abs((s.get("pnl_pct") or 0) - r.pnl_pct) < 1e-6:
            same += 1
            continue
        changes.append({"id": s["id"], "old": s["outcome"], "old_pnl": s.get("pnl_pct"), "new": r.status,
                        "new_pnl": r.pnl_pct, "window_min": w})
    Path(out_path).write_text(json.dumps({"computed_at": datetime.now(UTC).isoformat(), "changes": changes}))
    from collections import Counter
    print(f"{len(changes)} to change, {same} unchanged, skipped {skipped}")
    print("old -> new:", dict(Counter((c["old"], c["new"]) for c in changes)))


async def apply(path: str) -> None:
    from sqlalchemy import select, update

    from storage.database import AsyncSessionFactory
    from storage.models import CryptoSignalLog
    changes = json.loads(Path(path).read_text())["changes"]
    backup = Path.home() / "backups" / f"crypto_signal_log_{datetime.now(UTC):%Y%m%d_%H%M}.jsonl.gz"
    backup.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionFactory() as s:
        rows = (await s.execute(select(CryptoSignalLog))).scalars().all()
        with gzip.open(backup, "wt") as f:
            for r in rows:
                f.write(json.dumps({c.name: getattr(r, c.name) for c in CryptoSignalLog.__table__.columns},
                                   default=str) + "\n")
        print(f"backed up {len(rows)} rows to {backup}")
        done = 0
        for c in changes:
            res = await s.execute(update(CryptoSignalLog)
                                  .where(CryptoSignalLog.id == c["id"], CryptoSignalLog.outcome == c["old"])
                                  .values(outcome=c["new"], pnl_pct=c["new_pnl"]))
            done += res.rowcount or 0
        await s.commit()
    print(f"updated {done} of {len(changes)} rows")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "compute":
        compute(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == "apply":
        import asyncio
        asyncio.run(apply(sys.argv[2]))
    else:
        print(__doc__)
