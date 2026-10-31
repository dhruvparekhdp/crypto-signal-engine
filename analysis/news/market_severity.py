"""Re-score severity from BTC move (plan W10 preview, P2)."""


def market_severity_from_btc_move(move_2h_pct: float | None) -> int | None:
    if move_2h_pct is None:
        return None
    a = abs(move_2h_pct)
    if a >= 4.0:
        return 5
    if a >= 2.0:
        return 4
    if a >= 1.0:
        return 3
    if a >= 0.4:
        return 2
    return 1
