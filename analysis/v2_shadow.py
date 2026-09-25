"""
One live step of v2 shadow trading, as a pure function.

Given the latest closed frames for a symbol and the shadow rows still open,
return the new candidates to record and the updates to apply. The runner
does the fetching and the storing; this decides, with the backtest's own
code (`generate` for setups, `resolve_one` for fills and exits), so live
shadow results and backtest results are measured the same way.

Rules mirrored from the backtest:
  * one position per symbol: no new candidate while a shadow for the symbol
    is resting or open
  * only candidates decided on the latest closed 5m bars are new; older
    ones were either stored already or were missed while the job was down,
    and replaying them late would record fills that were never available
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis.v2_backtest import ExecConfig, resolve_one
from analysis.v2_setups import Candidate, V2Config, generate


@dataclass
class ShadowUpdate:
    row_id: int
    fields: dict


def row_to_candidate(row) -> Candidate:
    return Candidate(pd.Timestamp(row.decided_at), row.symbol, row.setup, row.side,
                     row.entry, row.stop, row.target)


def step(symbol: str, frames: dict, funding, open_rows: list, now: pd.Timestamp,
         cfg: V2Config = V2Config(), ex: ExecConfig = ExecConfig(),
         fresh_minutes: int = 10) -> tuple[list[Candidate], list[ShadowUpdate]]:
    k5 = frames.get("5m")
    if k5 is None or k5.empty:
        return [], []
    updates: list[ShadowUpdate] = []
    still_open = False
    for row in open_rows:
        cand = row_to_candidate(row)
        if cand.ts < k5["ts"].iloc[0]:
            updates.append(ShadowUpdate(row.id, {"status": "expired"}))
            continue
        status, trade = resolve_one(cand, k5, ex)
        if status == "cancelled":
            updates.append(ShadowUpdate(row.id, {"status": "cancelled"}))
        elif status == "closed":
            updates.append(ShadowUpdate(row.id, {
                "status": "closed",
                "filled_at": pd.Timestamp(trade.filled_at).to_pydatetime(),
                "exit_at": pd.Timestamp(trade.exit_at).to_pydatetime(),
                "exit_price": trade.exit, "reason": trade.reason, "r": trade.r}))
        else:
            still_open = True
            from analysis.v2_backtest import _arrays, _filled
            ts, h, lo, _, _ = _arrays(k5)
            if row.status == "pending" and _filled(cand, ts, h, lo, ex):
                updates.append(ShadowUpdate(row.id, {"status": "open"}))
    if still_open:
        return [], updates
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    k15, k4h, k1d = (frames.get(iv) if frames.get(iv) is not None else empty
                     for iv in ("15m", "4h", "1d"))
    cands = generate(symbol, k5, k15, k4h, k1d, funding, cfg)
    fresh = [c for c in cands if c.ts >= now - pd.Timedelta(minutes=fresh_minutes)]
    return fresh[-1:], updates      # at most one new position per symbol per step
