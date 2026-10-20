"""
Backtest = live check (owner, 19 Oct 2026: "go ahead with parity check"; the ideas are Freqtrade's lookahead /
recursive analysis and Passivbot's and NautilusTrader's one-code-path rule).

Signals - for every closed 4h / 8h bar of the last few days, three views of "did a swing strategy fire here":
  A  live     what the engine logged (crypto_signal_log, trade_mode swing)
  B  replay   the live computation re-run: analysis.swing_book.evaluate on the same Delta bars, the same 500-bar window
  W  warm     the same Delta bars with every bar of history fetched (a longer warm-up): B != W means an indicator has
              not settled inside 500 bars (Freqtrade's recursive analysis)
  C  backtest Binance bars with long history - what the 5-year backtest would have seen: W != C is the venue gap
A != B is the one that matters: the scanner missed a bar, fired on a bar it should not have, or logged a different
strategy or side. B-only signals whose stop would be too wide (levels() is None) or that the scan reached too late
are skipped by the live scan on purpose and are reported as such, not as faults.

Execution - every live Delta position against its paper copy: entry (bps), initial stop, target, contracts, the
current (ladder) stop and R now. They are the same trade; a gap is a fault.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from analysis import swing_book as sb
from analysis.lab import features as F
from analysis.lab.data import INTERVAL_MS, Bars
from analysis.lab.strategies import REGISTRY

LIVE_WINDOW = 500                      # swing_book.fetch_bars default limit: what the live scan evaluates on
_BAR_RE = re.compile(r"bar closed at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC")


@dataclass
class Fire:
    strategy: str
    side: int
    atr: float
    close: float


def _slice(b: Bars, a: int, z: int) -> Bars:
    return Bars(b.symbol, b.interval, b.t[a:z], b.o[a:z], b.h[a:z], b.l[a:z], b.c[a:z], b.v[a:z], b.tb[a:z])


def fires_windowed(b: Bars, specs, bar_opens: list[int], window: int | None) -> dict[int, Fire | None]:
    """evaluate() on the bars up to each wanted bar - the last `window` of them, or all when window is None."""
    idx = {int(t): i for i, t in enumerate(b.t)}
    out: dict[int, Fire | None] = {}
    for t in bar_opens:
        i = idx.get(int(t))
        if i is None:
            continue
        a = 0 if window is None else max(0, i + 1 - window)
        if window is not None and i + 1 - a < window:       # not enough history for the live view: not comparable
            continue
        s = sb.evaluate(_slice(b, a, i + 1), specs)
        out[int(t)] = Fire(s.strategy, s.side, s.atr, s.close) if s else None
    return out


def fires_full(b: Bars, specs, bar_opens: list[int]) -> dict[int, Fire | None]:
    """The backtest's view: each strategy over the whole series once (they are causal), first in priority order."""
    sigs = {(tf, sid, tuple(sorted(p.items()))): REGISTRY[sid].signals(b, p)
            for tf, sid, p in specs if tf == b.interval}
    atr = F.atr(b.h, b.l, b.c)
    idx = {int(t): i for i, t in enumerate(b.t)}
    out: dict[int, Fire | None] = {}
    for t in bar_opens:
        i = idx.get(int(t))
        if i is None:
            continue
        hit = None
        for tf, sid, p in specs:
            if tf != b.interval:
                continue
            s = int(sigs[(tf, sid, tuple(sorted(p.items())))][i])
            if s:
                hit = Fire(sid, s, float(atr[i]), float(b.c[i]))
                break
        out[int(t)] = hit
    return out


def logged_bar_open(row, tf: str) -> int | None:
    """Bar open time (ms) a logged swing signal fired on, from its trigger text ('bar closed at ... UTC')."""
    m = _BAR_RE.search(str(getattr(row, "trigger_description", "") or ""))
    if m:
        close = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        return int(close.timestamp() * 1000) - INTERVAL_MS[tf]
    ts = getattr(row, "timestamp", None)
    if ts is None:
        return None
    ms = int((ts if ts.tzinfo else ts.replace(tzinfo=UTC)).timestamp() * 1000)
    step = INTERVAL_MS[tf]
    return ms // step * step - step


@dataclass
class SignalParity:
    compared: int = 0                  # (coin, tf, bar) cells with a live view
    live_fired: int = 0
    replay_fired: int = 0
    faults: list[dict] = field(default_factory=list)       # A != B
    expected_skips: list[dict] = field(default_factory=list)
    warmup_diffs: list[dict] = field(default_factory=list)  # B != W
    venue_diffs: list[dict] = field(default_factory=list)   # W != C
    venue_cells: int = 0


def _same(a: Fire | None, b: Fire | None) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and a.strategy == b.strategy and a.side == b.side)


def _desc(f: Fire | None) -> str:
    return "nothing" if f is None else f"{f.strategy} {'long' if f.side > 0 else 'short'}"


def compare_signals(symbol: str, tf: str, bar_opens: list[int], live: dict[int, tuple[str, int]],
                    replay: dict[int, Fire | None], warm: dict[int, Fire | None], back: dict[int, Fire | None],
                    scan_started_ms: int | None, out: SignalParity) -> None:
    """Fold one coin / timeframe into `out`. live: {bar_open: (strategy, side)} from the log."""
    for t in bar_opens:
        if t not in replay:
            continue
        out.compared += 1
        b = replay[t]
        a = Fire(live[t][0], live[t][1], 0.0, 0.0) if t in live else None
        out.live_fired += a is not None
        out.replay_fired += b is not None
        when = datetime.fromtimestamp((t + INTERVAL_MS[tf]) / 1000, UTC).strftime("%d %b %H:%M")
        cell = {"symbol": symbol.upper(), "tf": tf, "bar_close": when, "live": _desc(a), "replay": _desc(b)}
        if not _same(a, b):
            if a is None and b is not None and sb.levels(b.close, b.side, b.atr) is None:
                out.expected_skips.append({**cell, "why": "stop would be wider than 8%: the live scan skips it"})
            elif a is None and scan_started_ms is not None and t + INTERVAL_MS[tf] < scan_started_ms:
                out.expected_skips.append({**cell, "why": "before the engine's first scan of this coin"})
            else:
                out.faults.append(cell)
        if t in warm and not _same(b, warm[t]):
            out.warmup_diffs.append({**cell, "warm": _desc(warm[t])})
        if t in warm and t in back:
            out.venue_cells += 1
            if not _same(warm[t], back[t]):
                out.venue_diffs.append({**cell, "backtest": _desc(back[t]), "delta": _desc(warm[t])})


def execution_parity(live_positions: dict, paper_rows: list, marks: dict, contract_of, live_wallet_inr: float = 0.0,
                     paper_equity_inr: float = 0.0, rate: float = 85.0, max_risk_pct: float = 5.0) -> list[dict]:
    """One row per live position, against the open paper swing position on the same coin and side. Size is compared
    as risk in % of each book's own wallet (paper has its own, larger wallet: 130 paper contracts and 11 live ones are
    the same 3% trade); whole contracts round live down, so only a risk above the round-up cap is a problem."""
    out = []
    for dsym, rec in (live_positions or {}).items():
        sym = str(rec.get("binance_symbol") or "").lower()
        side = 1 if rec.get("side") == "buy" else -1
        paper = next((p for p in paper_rows if p.symbol.lower() == sym and getattr(p, "trade_mode", "swing") == "swing"
                      and (1 if str(getattr(p.side, "value", p.side)) == "long" else -1) == side), None)
        row = {"symbol": dsym, "side": "long" if side > 0 else "short", "problems": []}
        if paper is None:
            row["problems"].append("no open paper position on this coin and side")
            out.append(row)
            continue
        try:
            fill = float(rec.get("entry_fill"))           # Delta's average fill; "filled" when it was not reported
        except (TypeError, ValueError):
            fill = float(rec.get("entry") or 0)
        p_entry = float(paper.entry_price)
        risk = abs(p_entry - float(paper.initial_stop_price or paper.stop_price))
        row["entry_gap_bps"] = round((fill - p_entry) / p_entry * 1e4 * side, 1) if p_entry else None
        for key, live_v, paper_v in (("initial stop", rec.get("init_stop"), paper.initial_stop_price or paper.stop_price),
                                     ("target", rec.get("target"), paper.target_price),
                                     ("current stop", rec.get("stop"), paper.stop_price)):
            if live_v is None or paper_v is None or not risk:
                continue
            gap = abs(float(live_v) - float(paper_v)) / risk
            row[f"{key} gap R"] = round(gap, 3)
            if gap > 0.02:
                row["problems"].append(f"{key}: live {float(live_v):.6g} vs paper {float(paper_v):.6g} ({gap:.2f}R)")
        cv = contract_of(sym) or 0
        if cv:
            row["contracts"] = {"live": rec.get("size"), "paper": round(float(paper.coin_qty) / cv, 4)}
        if cv and rec.get("size") and live_wallet_inr and paper_equity_inr:
            live_risk = float(rec["size"]) * cv * abs(fill - float(rec.get("init_stop") or fill)) * rate
            paper_risk = float(paper.coin_qty) * risk * float(getattr(paper, "usdt_inr", rate) or rate)
            lr, pr = live_risk / live_wallet_inr * 100, paper_risk / paper_equity_inr * 100
            row["risk_pct"] = {"live": round(lr, 2), "paper": round(pr, 2)}
            for who, v in (("live", lr), ("paper", pr)):
                if v > max_risk_pct + 0.1:
                    row["problems"].append(f"{who} risks {v:.1f}% of its wallet (cap {max_risk_pct:g}%)")
        mark = marks.get(sym.upper())
        if mark and risk and fill:
            live_r = side * (mark - fill) / abs(fill - float(rec.get("init_stop") or 0) or risk)
            paper_r = side * (mark - p_entry) / risk
            row["r_now"] = {"live": round(live_r, 3), "paper": round(paper_r, 3)}
            if abs(live_r - paper_r) > 0.10:
                row["problems"].append(f"R drift: live {live_r:+.2f}R vs paper {paper_r:+.2f}R")
        out.append(row)
    return out


def logged_live_fires(rows, tf: str) -> dict[tuple[str, int], tuple[str, int]]:
    """{(symbol, bar_open): (strategy, side)} for logged swing signals of one timeframe."""
    out = {}
    for r in rows:
        if str(getattr(r, "timeframe", "")) != tf:
            continue
        t = logged_bar_open(r, tf)
        if t is None:
            continue
        out[(r.symbol.upper(), t)] = (str(r.signal_type).replace("swing_", ""), 1 if r.direction == "long" else -1)
    return out


def recent_bar_opens(b: Bars, days: float, now_ms: int) -> list[int]:
    step = INTERVAL_MS[b.interval]
    lo = now_ms - days * 86_400_000
    return [int(t) for t in b.t if t >= lo and t + step <= now_ms]


def summary(sp: SignalParity, ex: list[dict]) -> dict:
    exec_problems = sum(len(r["problems"]) for r in ex)
    return {"signals_compared": sp.compared, "live_fired": sp.live_fired, "replay_fired": sp.replay_fired,
            "signal_faults": len(sp.faults), "expected_skips": len(sp.expected_skips),
            "warmup_diffs": len(sp.warmup_diffs),
            "venue_agreement_pct": round(100 * (1 - len(sp.venue_diffs) / sp.venue_cells), 1) if sp.venue_cells else None,
            "positions_checked": len(ex), "execution_problems": exec_problems,
            "ok": not sp.faults and not exec_problems}

