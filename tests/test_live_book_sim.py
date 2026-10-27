"""Live-book simulator (27 Oct 2026): the live book's rules replayed from a date with a wallet."""
import json

import pandas as pd
import pytest

from analysis.simulator import live_book as lb
from scripts.live_book_trades import drop_open, merge

H = 3_600_000
D0 = int(pd.Timestamp("2025-01-01", tz="UTC").timestamp() * 1000)


def trades(rows):
    """rows: (entry_h, exit_h, coin, entry, stop_frac, net_ret, r, spec)"""
    return pd.DataFrame([{"entry_t": D0 + a * H, "exit_t": D0 + b * H, "coin": c, "entry": e, "stop_frac": sf,
                          "net_ret": nr, "r_net": r, "side": -1, "spec": sp, "reason": "target" if r > 0 else "stop",
                          "filter_ok": True, "prio": 0.0, "symbol": c + "USDT", "tf": "4h", "strategy": "donchian",
                          "sig_t": D0 + a * H - 1}
                         for a, b, c, e, sf, nr, r, sp in rows])


def test_a_run_returns_the_wallet_path_months_and_every_trade():
    t = trades([(0, 10, "ADA", 0.21, 0.07, 0.21, 3.0, 2), (1, 30, "XRP", 1.26, 0.05, -0.05, -1.0, 3),
                (24 * 40, 24 * 41, "SUI", 0.97, 0.06, 0.18, 3.0, 2)])
    r = lb.run(t, start="2025-01-01", wallet=800, risk=0.03, max_open=4, round_up=0.10)
    s = r["summary"]
    assert s["taken"] == 3 and len(r["trades"]) == 3 and len(r["equity"]) == 3
    assert s["end_inr"] == pytest.approx(800 + sum(x["pnl_inr"] for x in r["trades"]), abs=0.05)
    assert r["equity"][-1][1] == pytest.approx(s["end_inr"], abs=0.01)
    assert [m["month"] for m in r["months"]] == ["2025-01", "2025-02"]
    assert r["trades"][0]["setup"] == "4h Donchian" and r["trades"][0]["side"] == "short"


def test_filters_by_date_strategy_and_coin_and_zero_means_no_open_limit():
    t = trades([(i, 100, c, 0.97, 0.06, 0.01, 0.17, i % 2) for i, c in enumerate(["ADA", "XRP", "SUI", "TIA", "AVAX"])])
    assert lb.run(t, max_open=2)["summary"]["taken"] == 2
    assert lb.run(t, max_open=0)["summary"]["taken"] == 5
    assert lb.run(t, specs=[1])["summary"]["signals"] == 2
    assert lb.run(t, coins=["ada", "xrp"])["summary"]["signals"] == 2
    assert lb.run(t, start="2025-02-01")["summary"]["signals"] == 0


def test_a_rebuild_keeps_older_trades_replaces_the_window_and_drops_trades_still_open():
    old = trades([(0, 5, "ADA", 0.2, 0.05, 0.01, 0.2, 0), (24 * 30, 24 * 31, "XRP", 1.2, 0.05, 0.01, 0.2, 0)])
    new = trades([(24 * 30, 24 * 32, "XRP", 1.2, 0.05, 0.02, 0.4, 0), (24 * 33, 24 * 34, "SUI", 0.9, 0.05, 0.01, 0.2, 0)])
    m = merge(old, new, D0 + 24 * 29 * H)
    assert list(m.coin) == ["ADA", "XRP", "SUI"] and m[m.coin == "XRP"].r_net.iloc[0] == pytest.approx(0.4)
    cut = trades([(0, 30, "ADA", 0.2, 0.05, 0.01, 0.2, 0), (0, 24 * 7, "XRP", 1.2, 0.05, 0.01, 0.2, 0)])
    cut["reason"] = "time"
    assert list(drop_open(cut).coin) == ["XRP"]          # 30 h "time" exit = the data ended; 7 days = a real exit


async def test_the_api_runs_validates_and_says_when_the_data_is_missing(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler import live_book_sim_api
    monkeypatch.setattr(lb, "TRADES_PATH", tmp_path / "t.parquet")
    monkeypatch.setattr(lb, "META_PATH", tmp_path / "m.json")
    app = web.Application()
    live_book_sim_api.register(app)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/sim/live-book")).status == 503
        trades([(0, 10, "ADA", 0.21, 0.07, 0.21, 3.0, 2)]).to_parquet(tmp_path / "t.parquet")
        (tmp_path / "m.json").write_text(json.dumps({"trades": 1, "refreshed_at": "2026-10-27T04:10:00Z"}))
        body = await (await client.get("/api/sim/live-book?start=2025-01-01&wallet=800&risk=3&max_open=4")).json()
        assert body["summary"]["taken"] == 1 and body["data"]["trades"] == 1 and body["params"]["risk"] == 0.03
        assert (await client.get("/api/sim/live-book?risk=50")).status == 400
        assert (await client.get("/api/sim/live-book?start=yesterday")).status == 400
        meta = await (await client.get("/api/sim/live-book/meta")).json()
        assert meta["coins"] == ["ADA"] and len(meta["specs"]) == 8


def test_the_book_hub_has_the_new_simulator_and_keeps_the_old_one():
    from pathlib import Path
    nav = Path("scheduler/portal/nav.js").read_text()
    assert '{ id: "simulator", label: "Simulator", native: true }' in nav
    assert '"/classic#simulator"' in nav
    js = Path("scheduler/portal/hubs/book.js").read_text()
    assert 'UI.view("book/simulator", [["sim-live"]])' in js and "/api/sim/live-book" in js


def test_a_coin_the_rebuild_could_not_cover_keeps_its_trades():
    old = trades([(24 * 30, 24 * 31, "XRP", 1.2, 0.05, 0.01, 0.2, 0), (24 * 30, 24 * 31, "TIA", 0.6, 0.05, 0.01, 0.2, 0)])
    new = trades([(24 * 33, 24 * 34, "XRP", 0.9, 0.05, 0.01, 0.2, 0)])
    m = merge(old, new, D0 + 24 * 29 * H, rebuilt={("XRPUSDT", "4h")})   # TIA had too little data to rebuild
    assert sorted(m.coin) == ["TIA", "XRP"] and len(m) == 2                 # old XRP replaced, TIA kept
