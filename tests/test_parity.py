"""Backtest = live check (owner, 19 Oct 2026: "go ahead with parity check")."""
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from analysis import parity as par
from analysis.lab.data import INTERVAL_MS, Bars

H4 = INTERVAL_MS["4h"]
SPECS = [("4h", "donchian", {"n": 20})]


def bars(n=700, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    o = np.r_[c[0], c[:-1]]
    h, lo = np.maximum(o, c) * 1.005, np.minimum(o, c) * 0.995
    t = np.arange(n, dtype=np.int64) * H4 + 1_700_000_000_000
    return Bars("ETHUSDT", "4h", t, o, h, lo, c, np.full(n, 10.0), np.full(n, 5.0))


def test_the_live_window_and_the_full_series_agree_for_a_causal_rule():
    b = bars()
    opens = [int(t) for t in b.t[-60:]]
    w, f = par.fires_windowed(b, SPECS, opens, par.LIVE_WINDOW), par.fires_full(b, SPECS, opens)
    assert w and all(par._same(w[t], f[t]) for t in w)
    assert any(v is not None for v in f.values())                   # the series does fire somewhere
    assert par.fires_windowed(bars(300), SPECS, opens[:1], par.LIVE_WINDOW) == {}   # too little history: no view


def _fire(side=1, atr=1.0, close=100.0):
    return par.Fire("donchian", side, atr, close)


def test_a_missed_or_extra_live_signal_is_a_fault_and_a_too_wide_stop_is_not():
    sp = par.SignalParity()
    t1, t2, t3, t4 = (1_700_000_000_000 + i * H4 for i in range(4))
    replay = {t1: _fire(), t2: None, t3: _fire(atr=4.0), t4: _fire()}   # t3: 3 x ATR 4 = 12% stop > 8% -> skipped
    live = {t1: ("donchian", 1), t2: ("donchian", -1)}                   # t2 fired live but not in the replay
    par.compare_signals("ETHUSDT", "4h", [t1, t2, t3, t4], live, replay, replay, replay, None, sp)
    close = lambda t: datetime.fromtimestamp((t + H4) / 1000, UTC).strftime("%d %b %H:%M")  # noqa: E731
    assert sp.compared == 4 and [f["bar_close"] for f in sp.faults] == [close(t2), close(t4)]
    assert len(sp.expected_skips) == 1 and "wider than 8%" in sp.expected_skips[0]["why"]


def test_warmup_and_venue_gaps_are_reported_separately():
    sp = par.SignalParity()
    t = 1_700_000_000_000
    par.compare_signals("ETHUSDT", "4h", [t], {t: ("donchian", 1)}, {t: _fire()}, {t: None}, {t: _fire(-1)}, None, sp)
    assert not sp.faults and len(sp.warmup_diffs) == 1 and len(sp.venue_diffs) == 1 and sp.venue_cells == 1


def test_the_bar_a_logged_signal_fired_on():
    row = SimpleNamespace(trigger_description="Donchian fired on the 4h bar closed at 2026-10-18 20:00 UTC; stop 3xATR",
                          timestamp=None)
    assert par.logged_bar_open(row, "4h") == int(datetime(2026, 10, 18, 16, 0, tzinfo=UTC).timestamp() * 1000)


def _paper(**kw):
    base = dict(symbol="xrpusdt", side="short", entry_price=1.2629, initial_stop_price=1.32622, stop_price=1.32622,
                target_price=1.07343, coin_qty=2.0, trade_mode="swing")
    base.update(kw)
    return SimpleNamespace(**base)


LIVE = {"XRPUSD": {"side": "sell", "size": 2, "entry": 1.26302, "entry_fill": "1.263", "stop": 1.32622,
                   "init_stop": 1.32622, "target": 1.07343, "binance_symbol": "xrpusdt"}}


def test_a_live_position_that_matches_its_paper_copy_has_no_problems():
    # 20 Oct 2026 production: 2 live contracts on Rs500 and 30 paper contracts on paper's larger wallet are one trade
    rows = par.execution_parity(LIVE, [_paper(coin_qty=30.0, usdt_inr=85.0)], {"XRPUSDT": 1.3032}, lambda s: 1.0,
                                live_wallet_inr=499.8, paper_equity_inr=5500.0)
    r = rows[0]
    assert r["problems"] == [] and r["contracts"] == {"live": 2, "paper": 30.0} and abs(r["entry_gap_bps"]) < 1
    assert r["risk_pct"]["live"] == pytest.approx(2.15, abs=0.02) and r["risk_pct"]["paper"] == pytest.approx(2.94, abs=0.02)


def test_risk_over_the_cap_stop_gaps_and_missing_paper_are_problems():
    rows = par.execution_parity(LIVE, [_paper(coin_qty=300.0, stop_price=1.30, usdt_inr=85.0)], {}, lambda s: 1.0,
                                live_wallet_inr=499.8, paper_equity_inr=5500.0)
    assert any(p.startswith("paper risks") for p in rows[0]["problems"])          # 29% of the wallet
    assert any(p.startswith("current stop") for p in rows[0]["problems"])          # ladder moved paper, not live
    assert par.execution_parity(LIVE, [], {}, lambda s: 1.0)[0]["problems"] == ["no open paper position on this coin and side"]


async def test_the_daily_job_runs_end_to_end_and_writes_its_report(tmp_path, monkeypatch):
    import json

    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis import swing_book as sb
    from execution import delta_book
    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", async_sessionmaker(engine, expire_on_commit=False))

    def series(sym, tf):
        b = bars(1100, seed=len(sym))
        step = INTERVAL_MS[tf]
        now = int(datetime.now(UTC).timestamp() * 1000) // step * step
        t = now - step * np.arange(len(b.t))[::-1] - step
        return Bars(sym, tf, t, b.o, b.h, b.l, b.c, b.v, b.tb)

    async def fake_fetch(client, sym, now_ms, tf, limit=500):
        return series(sym, tf)

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    async def fake_get(self, url, **kw):
        fake_get.tf = kw["params"]["interval"]
        fake_get.sym = kw["params"]["symbol"]
        return Resp()
    monkeypatch.setattr(sb, "fetch_bars", fake_fetch)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(sb, "bars_from_klines", lambda sym, rows, now_ms, tf: series(sym, tf))
    monkeypatch.setattr(delta_book, "STATE_PATH", tmp_path / "none.json")
    r = AppRunner()
    r._PARITY_FILE = str(tmp_path / "parity.json")

    async def no_marks():
        return {}
    r._perp_prices = no_marks
    rep = await r._parity_job(days=3, alert=False)
    sm = rep["summary"]
    assert sm["signals_compared"] > 0 and sm["warmup_diffs"] >= 0 and sm["venue_agreement_pct"] == 100.0
    # nothing was logged live, so every bar the replay fired on (with a valid stop) is a mismatch
    assert sm["signal_faults"] + sm["expected_skips"] == sm["replay_fired"]
    assert json.loads((tmp_path / "parity.json").read_text())["summary"] == sm
