"""
Live-book simulator (owner, 27 Oct 2026: "simulator can be upgraded" ... "you develop and deploy after testing").

Replays what the LIVE book would have done from a chosen date with a chosen wallet - not the old 5-minute intraday
detectors. Everything is the live code or the lab exit the live book was built from:
  signals   the 8 live swing specs (4h / 8h vol breakout, Keltner, Donchian, Ichimoku) on the 12 live coins
  exit      lab rb_std_c60_b25 = the live exit: 3xATR stop, 3R target, profit ladder 1/1.5/2/2.5R with the 0.25R
            buffer and 1h-close confirmation, 7-day limit; Delta fees (taker + GST + slippage) and real funding
  rules     scripts/live_sim.simulate: the live sizing (whole Delta contracts, the one-contract round-up, leverage by
            stop distance, margin cap), the BTC volatility filter, one trade per coin, max open, 10% daily loss
            limit, floor at half the starting wallet; no news blackout (owner, 27 Oct 2026)
Trades come from data/sim/live_book_trades.parquet (scripts/live_book_trades.py: a full build on a machine with the
5-year lake, the last 45 days rebuilt nightly on the server). A trade still open when the data ends is left out
until it closes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

TRADES_PATH = Path("data/sim/live_book_trades.parquet")
META_PATH = Path("data/sim/live_book_meta.json")
SPEC_LABELS = ["4h vol breakout", "4h Keltner", "4h Donchian", "4h Ichimoku",
               "8h Keltner", "8h vol breakout", "8h Donchian", "8h Ichimoku"]
_cache: dict = {"mtime": None, "t": None}


def load(path: Path | None = None) -> pd.DataFrame:
    """The prepared trade set (cached until the file changes). Empty when it has not been built yet."""
    path = path or TRADES_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return pd.DataFrame()
    if _cache["mtime"] != (str(path), mtime):
        t = pd.read_parquet(path)
        _cache.update(mtime=(str(path), mtime), t=t.sort_values(["entry_t", "prio"], ascending=[True, False])
                      .reset_index(drop=True))
    return _cache["t"]


def meta(path: Path | None = None) -> dict:
    try:
        return json.loads((path or META_PATH).read_text())
    except (OSError, ValueError):
        return {}


def _ms(day: str | None) -> int | None:
    return int(pd.Timestamp(day, tz="UTC").timestamp() * 1000) if day else None


def run(t: pd.DataFrame, start: str | None = None, end: str | None = None, wallet: float = 800.0, risk: float = 0.03,
        max_open: int = 4, round_up: float = 0.10, specs: list[int] | None = None,
        coins: list[str] | None = None) -> dict:
    """One replay. Returns the summary, the equity curve (wallet after each closed trade), months, every trade and
    the reasons signals were not taken."""
    from scripts.live_sim import simulate
    q = t
    a, b = _ms(start), _ms(end)
    if a is not None:
        q = q[q.entry_t >= a]
    if b is not None:
        q = q[q.entry_t < b + 86_400_000]
    if specs is not None:
        q = q[q.spec.isin(specs)]
    if coins is not None:
        q = q[q.coin.isin([c.upper() for c in coins])]
    r = simulate(q, float(wallet), float(risk), round_up=float(round_up), max_open=int(max_open) if max_open else 99,
                 keep_trades=True)
    closed = sorted(r.pop("trades", []), key=lambda p: p["exit_t"])
    w, eq, rows = float(wallet), [], []
    for p in closed:
        w += p["pnl"]
        eq.append([int(p["exit_t"]), round(w, 2)])
        rows.append({"entry_t": int(p["entry_t"]), "exit_t": int(p["exit_t"]), "coin": p["coin"],
                     "side": "long" if p["side"] > 0 else "short", "setup": SPEC_LABELS[p["spec"]],
                     "contracts": int(p["contracts"]), "risk_inr": round(p["risk_inr"], 2), "r": round(p["r"], 2),
                     "pnl_inr": round(p["pnl"], 2), "reason": p["reason"], "wallet_after": round(w, 2)})
    months = []
    if rows:
        df = pd.DataFrame(rows)
        df["month"] = pd.to_datetime(df.exit_t, unit="ms").dt.strftime("%Y-%m")
        for m, g in df.groupby("month"):
            months.append({"month": m, "trades": len(g), "won": int((g.pnl_inr > 0).sum()),
                           "pnl_inr": round(float(g.pnl_inr.sum()), 2), "wallet_end": float(g.wallet_after.iloc[-1])})
    first = int(q.entry_t.min()) if len(q) else None
    return {"params": {"start": start, "end": end, "wallet": wallet, "risk": risk, "max_open": max_open,
                       "round_up": round_up, "specs": specs, "coins": coins},
            "summary": {**r, "start_inr": float(wallet), "end_inr": round(w, 2), "pnl_inr": round(w - float(wallet), 2),
                        "signals": int(len(q)), "first_signal_ms": first},
            "equity": eq, "months": months, "trades": rows,
            "generated": datetime.now(UTC).isoformat(timespec="seconds")}
