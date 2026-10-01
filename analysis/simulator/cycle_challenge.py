"""
Cycle & Challenge Compounding Simulator.

Simulates compounding runs:
- Start with 25 USDT
- Reach 100 USDT -> Target Reached -> Reset to 25 USDT & start next cycle
- Reach 0 USDT (or < min margin) -> Busted -> Reset to 25 USDT & start next cycle
- Produces a comprehensive cycle summary and granular account statement for every cycle.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Sequence

from analysis.simulator.replay_engine import SimulatedTrade


@dataclass
class CycleTradeTransaction:
    tx_id: int
    trade_id: int
    timestamp: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    exit_reason: str
    margin_usdt: float
    leverage: float
    notional_usdt: float
    fee_usdt: float
    price_change_pct: float
    gross_pnl_usdt: float
    net_pnl_usdt: float
    pnl_pct_on_margin: float
    balance_before: float
    balance_after: float


@dataclass
class CycleStatement:
    cycle_id: int
    status: str  # "TARGET_REACHED" | "BUSTED" | "IN_PROGRESS"
    start_time: str
    end_time: str
    duration_str: str
    starting_balance: float
    ending_balance: float
    peak_balance: float
    max_drawdown_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_profit_usdt: float
    gross_loss_usdt: float
    total_fees_usdt: float
    net_profit_usdt: float
    transactions: list[CycleTradeTransaction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_str": self.duration_str,
            "starting_balance": round(self.starting_balance, 2),
            "ending_balance": round(self.ending_balance, 2),
            "peak_balance": round(self.peak_balance, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 1),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "gross_profit_usdt": round(self.gross_profit_usdt, 2),
            "gross_loss_usdt": round(self.gross_loss_usdt, 2),
            "total_fees_usdt": round(self.total_fees_usdt, 2),
            "net_profit_usdt": round(self.net_profit_usdt, 2),
            "transactions": [asdict(t) for t in self.transactions],
        }


def _calc_duration_str(start_iso: str, end_iso: str) -> str:
    try:
        dt1 = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        dt2 = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        diff = dt2 - dt1
        days = diff.days
        hours = int(diff.seconds // 3600)
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h {int((diff.seconds % 3600) // 60)}m"
    except Exception:
        return "—"


def run_cycle_simulation(
    trades: Sequence[SimulatedTrade],
    start_balance: float = 25.0,
    target_balance: float = 100.0,
    margin_pct: float = 0.25,
    leverage: float = 10.0,
    taker_fee_pct: float = 0.00059,  # 0.05% base + 18% GST = 0.059%
    maker_fee_pct: float = 0.000236, # 0.02% base + 18% GST = 0.0236%
    min_trade_margin: float = 1.0,
) -> dict[str, Any]:
    """
    Simulates paper trading cycles compounding from start_balance to target_balance,
    resetting to start_balance on reaching target OR hitting bust (0 balance).
    """
    if not trades:
        return {
            "total_cycles": 0,
            "targets_hit": 0,
            "busted": 0,
            "in_progress": 0,
            "cycle_win_rate_pct": 0.0,
            "total_net_profit_usdt": 0.0,
            "avg_trades_per_cycle": 0.0,
            "cycles": [],
        }

    # Sort trades chronologically
    sorted_trades = sorted(trades, key=lambda t: str(t.entry_time))

    cycles: list[CycleStatement] = []
    current_cycle_id = 1
    current_balance = float(start_balance)
    current_txs: list[CycleTradeTransaction] = []
    peak_balance = float(start_balance)
    max_drawdown = 0.0
    tx_counter = 1

    for t in sorted_trades:
        # Check if already busted before trade
        if current_balance < min_trade_margin:
            # Finalize busted cycle
            end_t = current_txs[-1].timestamp if current_txs else t.entry_time
            start_t = current_txs[0].timestamp if current_txs else t.entry_time
            stmt = _build_statement(
                cycle_id=current_cycle_id,
                status="BUSTED",
                start_time=start_t,
                end_time=end_t,
                start_balance=start_balance,
                ending_balance=current_balance,
                peak_balance=peak_balance,
                max_drawdown_pct=max_drawdown,
                txs=current_txs,
            )
            cycles.append(stmt)
            # Reset for next cycle
            current_cycle_id += 1
            current_balance = float(start_balance)
            peak_balance = float(start_balance)
            max_drawdown = 0.0
            current_txs = []

        # Calculate margin for this trade
        margin_usdt = round(current_balance * margin_pct, 2)
        if margin_usdt < min_trade_margin:
            margin_usdt = min(current_balance, min_trade_margin * 2.0)
        margin_usdt = min(margin_usdt, current_balance)

        notional = margin_usdt * leverage

        # Fee calculations
        entry_fee = notional * taker_fee_pct
        is_target_exit = "TAKE_PROFIT" in t.exit_reason or "TARGET" in t.exit_reason
        exit_fee_rate = maker_fee_pct if is_target_exit else taker_fee_pct
        exit_fee = notional * exit_fee_rate
        total_fee = round(entry_fee + exit_fee, 4)

        # PnL calculations
        # price_pnl_pct is the move on underlying price (e.g. +2.0% or -1.2%)
        price_pnl_pct = t.pnl_pct
        gross_pnl = round(margin_usdt * (price_pnl_pct / 100.0) * leverage, 2)
        net_pnl = round(gross_pnl - total_fee, 2)
        pnl_pct_on_margin = round((net_pnl / max(0.01, margin_usdt)) * 100.0, 1)

        bal_before = current_balance
        bal_after = max(0.0, round(current_balance + net_pnl, 2))
        current_balance = bal_after

        # Update peak & drawdown
        if current_balance > peak_balance:
            peak_balance = current_balance
        dd = (peak_balance - current_balance) / max(0.01, peak_balance) * 100.0
        if dd > max_drawdown:
            max_drawdown = dd

        tx = CycleTradeTransaction(
            tx_id=tx_counter,
            trade_id=t.trade_id,
            timestamp=str(t.entry_time),
            symbol=t.symbol,
            direction=t.direction,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            exit_reason=t.exit_reason,
            margin_usdt=margin_usdt,
            leverage=leverage,
            notional_usdt=round(notional, 2),
            fee_usdt=total_fee,
            price_change_pct=price_pnl_pct,
            gross_pnl_usdt=gross_pnl,
            net_pnl_usdt=net_pnl,
            pnl_pct_on_margin=pnl_pct_on_margin,
            balance_before=bal_before,
            balance_after=bal_after,
        )
        current_txs.append(tx)
        tx_counter += 1

        # Check Target Reached
        if current_balance >= target_balance:
            stmt = _build_statement(
                cycle_id=current_cycle_id,
                status="TARGET_REACHED",
                start_time=current_txs[0].timestamp,
                end_time=tx.timestamp,
                start_balance=start_balance,
                ending_balance=current_balance,
                peak_balance=peak_balance,
                max_drawdown_pct=max_drawdown,
                txs=current_txs,
            )
            cycles.append(stmt)
            # Reset
            current_cycle_id += 1
            current_balance = float(start_balance)
            peak_balance = float(start_balance)
            max_drawdown = 0.0
            current_txs = []

        # Check Bust
        elif current_balance < min_trade_margin:
            stmt = _build_statement(
                cycle_id=current_cycle_id,
                status="BUSTED",
                start_time=current_txs[0].timestamp,
                end_time=tx.timestamp,
                start_balance=start_balance,
                ending_balance=current_balance,
                peak_balance=peak_balance,
                max_drawdown_pct=max_drawdown,
                txs=current_txs,
            )
            cycles.append(stmt)
            # Reset
            current_cycle_id += 1
            current_balance = float(start_balance)
            peak_balance = float(start_balance)
            max_drawdown = 0.0
            current_txs = []

    # If active cycle remains at the end
    if current_txs:
        stmt = _build_statement(
            cycle_id=current_cycle_id,
            status="IN_PROGRESS",
            start_time=current_txs[0].timestamp,
            end_time=current_txs[-1].timestamp,
            start_balance=start_balance,
            ending_balance=current_balance,
            peak_balance=peak_balance,
            max_drawdown_pct=max_drawdown,
            txs=current_txs,
        )
        cycles.append(stmt)

    # Compute overall cycle summary
    targets_hit = sum(1 for c in cycles if c.status == "TARGET_REACHED")
    busted = sum(1 for c in cycles if c.status == "BUSTED")
    in_prog = sum(1 for c in cycles if c.status == "IN_PROGRESS")
    completed = targets_hit + busted
    win_rate = round(targets_hit / max(1, completed) * 100.0, 1)

    total_net_profit = sum(c.net_profit_usdt for c in cycles)
    avg_trades = round(sum(c.total_trades for c in cycles) / max(1, len(cycles)), 1)

    return {
        "start_capital": start_balance,
        "target_capital": target_balance,
        "margin_pct": margin_pct,
        "leverage": leverage,
        "total_cycles": len(cycles),
        "targets_hit": targets_hit,
        "busted": busted,
        "in_progress": in_prog,
        "cycle_win_rate_pct": win_rate,
        "total_net_profit_usdt": round(total_net_profit, 2),
        "avg_trades_per_cycle": avg_trades,
        "cycles": [c.to_dict() for c in cycles],
    }


def _build_statement(
    cycle_id: int,
    status: str,
    start_time: str,
    end_time: str,
    start_balance: float,
    ending_balance: float,
    peak_balance: float,
    max_drawdown_pct: float,
    txs: list[CycleTradeTransaction],
) -> CycleStatement:
    wins = sum(1 for tx in txs if tx.net_pnl_usdt > 0)
    losses = sum(1 for tx in txs if tx.net_pnl_usdt <= 0)
    total_trades = len(txs)
    win_rate = round(wins / max(1, total_trades) * 100.0, 1)

    gross_profit = sum(tx.gross_pnl_usdt for tx in txs if tx.gross_pnl_usdt > 0)
    gross_loss = abs(sum(tx.gross_pnl_usdt for tx in txs if tx.gross_pnl_usdt < 0))
    total_fees = sum(tx.fee_usdt for tx in txs)
    net_profit = ending_balance - start_balance

    duration_str = _calc_duration_str(start_time, end_time)

    return CycleStatement(
        cycle_id=cycle_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        duration_str=duration_str,
        starting_balance=start_balance,
        ending_balance=ending_balance,
        peak_balance=peak_balance,
        max_drawdown_pct=max_drawdown_pct,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate_pct=win_rate,
        gross_profit_usdt=gross_profit,
        gross_loss_usdt=gross_loss,
        total_fees_usdt=total_fees,
        net_profit_usdt=net_profit,
        transactions=list(txs),
    )
