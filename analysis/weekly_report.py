"""Roadmap F-2: the weekly forward report (Telegram, Monday 09:00 IST).

Forward results per strategy since its live_from, against its backtest, plus the wallet, withdrawals and why
signals were skipped. Plain text so it reads well in Telegram."""
from __future__ import annotations

from analysis import strategy_registry as reg
from analysis.swing_book import STRATEGY_R


def format_weekly_report(registry: dict, trades: list, equity: float, start: float, withdrawn: float,
                         week_trades: list, skips: dict[str, int]) -> str:
    lines = ["📊 Weekly swing report (paper)",
             f"Equity ₹{equity:,.0f} (started ₹{start:,.0f}) · withdrawn ₹{withdrawn:,.0f}"]
    wk = [reg.trade_r(t) for t in week_trades]
    lines.append(f"This week: {len(wk)} closed, {sum(1 for r in wk if r > 0)} won, "
                 f"{sum(wk):+.1f}R" if wk else "This week: no closed swing trades")
    lines.append("")
    lines.append("Strategy · status · forward trades · forward R (avg) · backtest avg · drawdown/alarm")
    for key, row in sorted(registry.items()):
        rs = reg.forward(trades, row)
        tf, sid = key.split("@", 1)
        bt = STRATEGY_R.get((sid, tf))
        avg = f"{sum(rs) / len(rs):+.2f}" if rs else "–"
        lines.append(f"{key} · {row.status} · {len(rs)} · {sum(rs):+.1f}R ({avg}) · "
                     f"{'+' if bt and bt > 0 else ''}{bt if bt is not None else '–'} · "
                     f"{reg.drawdown_r(rs):.1f}/{reg.alarm_limit(key):g}R")
    if skips:
        lines.append("")
        lines.append("Skipped signals this week: " + ", ".join(f"{k} {v}" for k, v in
                                                              sorted(skips.items(), key=lambda kv: -kv[1])[:6]))
    lines.append("")
    lines.append("Judge a strategy after ~30 forward trades; until then its average swings more than its edge.")
    return "\n".join(lines)
