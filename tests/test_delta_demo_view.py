"""15 Oct 2026: demo positions shown next to the real price (the testnet drifted +5% on SOL)."""
from scheduler.delta_api import demo_rows


def test_demo_row_shows_the_gap_and_demo_pnl():
    pos = {"SOLUSD": {"side": "sell", "entry_fill": "108.088", "size": 1, "binance_symbol": "solusdt",
                      "exchange_stop": 114.157, "target": 88.05}}
    r = demo_rows(pos, {"SOLUSD": 109.15}, {"SOLUSD": 103.94}, {"SOLUSD": 1.0})[0]
    assert r["gap_pct"] == round((109.15 - 103.94) / 103.94 * 100, 2)        # +5.01%
    assert r["demo_pnl_usd"] == round(-(109.15 - 108.088), 2)                   # short, demo price rose
    assert r["side"] == "short" and r["real_mark"] == 103.94 and "+5.0%" in r["note"]


def test_contract_value_scales_demo_pnl():
    pos = {"DOGEUSD": {"side": "sell", "entry_fill": 0.08478, "size": 23, "binance_symbol": "dogeusdt"}}
    r = demo_rows(pos, {"DOGEUSD": 0.08488}, {"DOGEUSD": 0.0819}, {"DOGEUSD": 100.0})[0]
    assert r["demo_pnl_usd"] == round(-(0.08488 - 0.08478) * 23 * 100, 2)
