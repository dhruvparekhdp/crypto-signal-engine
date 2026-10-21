"""Channel-style bracket, paper research (21 Oct 2026)."""
from datetime import datetime, timedelta

import pytest

import numpy as np

from analysis.bracket_book import advance, open_trade, summary, wallet_view
from analysis.level_signals import pivots, price_labels, setup_at

T0 = datetime(2026, 10, 21, 12, 0, 30)


def bars(rows, start=T0.replace(second=0) + timedelta(minutes=1)):
    return [{"timestamp": start + timedelta(minutes=i), "open": r[0], "high": r[1], "low": r[2], "close": r[3]}
            for i, r in enumerate(rows)]


def trade(side=1):
    return open_trade(7, "ethusdt", side, "confluence", "primary", "intraday", T0, 100.0)


def test_first_target_moves_the_stop_to_entry_then_a_pullback_closes_at_entry():
    tr = advance(trade(), bars([(100, 100.2, 99.9, 100.1), (100.1, 101.1, 100.0, 101.0), (101, 102.8, 100.9, 102.7),
                                (102.7, 102.8, 99.9, 100.0)]))
    assert tr["status"] == "closed" and tr["exit"] == "stop at entry" and tr["legs"] == 2 and tr["tp1"]
    assert tr["net_pct"] == pytest.approx((0.0101 + 0.0276) / 4 * 100 - 0.15)
    assert tr["roi_5x_pct"] == pytest.approx(tr["net_pct"] * 5, abs=0.01)


def test_a_bar_touching_both_before_the_first_target_is_the_full_stop():
    tr = advance(trade(), bars([(100, 101.5, 94.9, 101.2)]))
    assert tr["exit"] == "stop" and tr["net_pct"] == pytest.approx(-5.15) and not tr["tp1"]


def test_shorts_mirror_longs_and_the_48h_limit_closes_the_rest():
    tr = advance(trade(-1), bars([(100, 100.1, 98.9, 99.0)] + [(99.0, 99.2, 98.8, 99.0)] * (48 * 60)))
    assert tr["legs"] == 1 and tr["exit"] == "48h limit"
    assert tr["net_pct"] == pytest.approx((0.0101 / 4 + 0.75 * 0.01) * 100 - 0.15, abs=1e-6)


def test_it_waits_for_bars_after_the_signal_and_resumes_where_it_stopped():
    tr = trade()
    assert advance(tr, [])["status"] == "waiting"
    tr = advance(tr, bars([(100, 100.5, 99.5, 100.2)]))
    assert tr["status"] == "open" and tr["entry"] == 100
    tr = advance(tr, bars([(100, 100.5, 99.5, 100.2), (100.2, 101.2, 100.1, 101.0)]))   # first bar not re-read
    assert tr["legs"] == 1 and tr["stop"] == 100


def test_summary_groups_by_source_role_and_coin():
    a = advance(trade(), bars([(100, 101.5, 99.9, 101.2)] * 2))
    b = advance(open_trade(8, "ethusdt", -1, "confluence", "mirror", "intraday", T0, 100.0), bars([(100, 105.5, 99.9, 105)]))
    s = summary([a, b])
    assert s["all"]["n"] == 2 and s["role:mirror"]["full_stops"] == 1 and s["role:primary"]["tp1_pct"] == 100.0


async def test_the_job_opens_a_trade_per_new_signal_and_closes_it_on_candles(tmp_path, monkeypatch):
    import json

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis.crypto_state import CryptoState, OHLCVCandle
    from scheduler.runner import AppRunner
    from storage.models import Base, CryptoSignalLog
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", sm)
    from config.settings import settings
    monkeypatch.setattr(settings, "bracket_levels_enabled", False)       # the S/R scan has its own test below
    r = AppRunner()
    r._BRACKET_OPEN, r._BRACKET_CLOSED = str(tmp_path / "book.json"), str(tmp_path / "closed.jsonl")
    log = dict(signal_type="confluence", trigger_description="x", confidence=0.6, current_price=100.0, target_price=101,
               stop_loss=99, edge_pct=0, stake_pct=0, timeframe="1h")
    async with sm() as s:
        await Repository(s).log_crypto_signal(symbol="ethusdt", direction="long", **log)
    now = datetime(2026, 10, 21, 12, 30)
    await r._bracket_paper_job(now)                               # first run: starts after the newest signal
    async with sm() as s:
        await Repository(s).log_crypto_signal(symbol="ethusdt", direction="long", **log)
        await s.execute(update(CryptoSignalLog).where(CryptoSignalLog.id == 2).values(timestamp=now))
        await s.commit()
    st = CryptoState(symbol="ethusdt", base_asset="ETH")
    st.candles_1m = [OHLCVCandle(100, 100.2, 99.9, 100.1, 1, now + timedelta(minutes=1)),
                     OHLCVCandle(100.1, 101.2, 95.0, 96.0, 1, now + timedelta(minutes=2))]   # touches both: stop
    r.crypto_store._states["ethusdt"] = st
    out = await r._bracket_paper_job(now + timedelta(minutes=5))
    assert out == {"new": 1, "open": 0, "closed": 1}
    closed = [json.loads(ln) for ln in open(tmp_path / "closed.jsonl")]
    assert closed[0]["exit"] == "stop" and closed[0]["role"] == "primary" and closed[0]["source"] == "intraday"


# ---------------------------------------------------------------- S/R setups and the wallet view (21 Oct 2026)

def _flat(n=61, lo_at=None, hi_at=None):
    o = np.full(n, 103.0); c = o.copy(); h = np.full(n, 103.5); lo = np.full(n, 102.5)
    for i, v in (lo_at or {}).items():
        lo[i] = v
    for i, v in (hi_at or {}).items():
        h[i] = v
    return o, h, lo, c


def test_a_retest_of_a_two_touch_support_is_a_long_support_reclaim():
    o, h, lo, c = _flat(lo_at={20: 100.0, 40: 100.05})
    o[60], h[60], lo[60], c[60] = 100.3, 101.0, 100.1, 100.9
    ph, pl = pivots(h, lo)
    s = setup_at(60, o, h, lo, c, ph, pl)
    assert s["side"] == 1 and s["setup"] == "support reclaim" and s["touches"] == 2 and not s["swept"]
    c[60] = 100.2                                                     # a down candle at the level is no setup
    o[60] = 100.5
    assert setup_at(60, o, h, lo, c, ph, pl) is None


def test_old_resistance_retested_from_above_is_a_breakout_retest_and_a_wick_through_is_a_sweep():
    o, h, lo, c = _flat(hi_at={20: 105.0, 40: 105.1})
    o[60], h[60], lo[60], c[60] = 105.2, 106.0, 104.7, 105.9
    ph, pl = pivots(h, lo)
    s = setup_at(60, o, h, lo, c, ph, pl)
    assert s["side"] == 1 and s["setup"] == "breakout retest" and s["swept"]


def test_rejection_under_resistance_is_a_short():
    o, h, lo, c = _flat(hi_at={20: 105.0, 40: 105.1})
    for i in range(41, 60):
        o[i], h[i], lo[i], c[i] = 104.0, 104.4, 103.6, 104.0
    o[60], h[60], lo[60], c[60] = 104.5, 105.05, 104.0, 104.1
    ph, pl = pivots(h, lo)
    s = setup_at(60, o, h, lo, c, ph, pl)
    assert s["side"] == -1 and s["setup"] == "resistance reject"


def test_price_labels_are_signed_by_the_trade_side():
    c = np.linspace(100, 110, 30)
    ema, atr = np.full(30, 105.0), np.full(30, 0.8)
    assert price_labels(29, c, 1, ema, atr) == {"move_24h": "with", "trend_1h_ema200": "with", "atr_1h": "mid 0.5-1%"}
    assert price_labels(29, c, -1, ema, atr)["move_24h"] == "against"


def test_wallet_view_uses_fixed_margin_and_skips_only_without_free_margin():
    def t(entry_min, close_min, net):
        e = (T0 + timedelta(minutes=entry_min)).isoformat()
        out = {"entry_ts": e, "status": "closed" if close_min else "open", "net_pct": net}
        if close_min:
            out["closed_ts"] = (T0 + timedelta(minutes=close_min)).isoformat()
        return out
    rows = [t(0, 60, 1.0), t(10, 120, -5.15), t(30, 90, 2.0), t(180, None, 0.0)]
    w = wallet_view(rows, 1000, 500, 5, T0 + timedelta(hours=5))
    assert w["taken"] == 3 and w["skipped_no_margin"] == 1 and w["open"] == 1
    assert w["won"] == 1 and w["lost"] == 1
    assert w["wallet"] == pytest.approx(1000 + 25 - 128.75)
    assert w["max_dd_pct"] == pytest.approx(12.6, abs=0.05)


def test_summary_splits_sr_trades_by_label_picks_apart_from_mirrors():
    a = advance(open_trade("lv:a", "ethusdt", 1, "levels_support_reclaim", "primary", "levels", T0, 100.0,
                           labels={"coin_news": "with"}), bars([(100, 101.5, 99.9, 101.2)] * 2))
    b = advance(open_trade("lv:b", "ethusdt", -1, "levels_support_reclaim", "mirror", "levels", T0, 100.0,
                           labels={"coin_news": "against"}), bars([(100, 105.5, 99.9, 105)]))
    s = summary([a, b])
    assert s["label:primary|coin_news|with"]["n"] == 1 and s["label:mirror|coin_news|against"]["full_stops"] == 1
    assert s["source_role:levels|primary"]["tp1_pct"] == 100.0


async def test_the_hourly_scan_opens_a_labelled_pick_and_mirror_once_per_setup(tmp_path, monkeypatch):
    import json

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis import level_signals, swing_book
    from analysis.crypto_state import CryptoState
    from analysis.lab.data import Bars
    from scheduler.runner import AppRunner
    from storage.models import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", sm)
    n = 300
    t0 = int(datetime(2026, 10, 9).timestamp() * 1000)
    px = np.full(n, 2000.0)
    fake = Bars("ETHUSDT", "1h", t0 + 3_600_000 * np.arange(n, dtype=np.int64), px, px + 5, px - 5, px, px, px)
    calls = []

    async def fetch(client, symbol, now_ms, tf="4h", limit=500):
        calls.append((symbol, tf))
        return Bars(symbol, "1h", fake.t, fake.o, fake.h, fake.l, fake.c, fake.v, fake.tb)
    monkeypatch.setattr(swing_book, "fetch_bars", fetch)
    monkeypatch.setattr(level_signals, "setup_at", lambda *a: {"side": 1, "setup": "support reclaim", "level": 1990.0,
                                                               "touches": 3, "swept": True, "level_dist_pct": 0.5})
    r = AppRunner()
    r._BRACKET_OPEN, r._BRACKET_CLOSED = str(tmp_path / "book.json"), str(tmp_path / "closed.jsonl")

    async def no_bars(*a):
        return []
    r._signal_bars = no_bars
    st = CryptoState(symbol="ethusdt", base_asset="ETH")
    st.sentiment_score, st.sentiment_news_count, st.funding_rate_per_8h = 0.5, 3, 0.0003
    r.crypto_store._states["ethusdt"] = st
    await r._bracket_paper_job(datetime(2026, 10, 21, 12, 5))
    book = json.loads((tmp_path / "book.json").read_text())
    lv = [t for t in book["open"] if t["source"] == "levels"]
    assert [(t["role"], t["side"]) for t in lv] == [("primary", 1), ("mirror", -1)] and calls == [("ETHUSDT", "1h")]
    pick, mirror = lv
    assert pick["labels"]["setup"] == "support reclaim" and pick["labels"]["level"] == "swept"
    assert pick["labels"]["coin_news"] == "with" and mirror["labels"]["coin_news"] == "against"
    assert pick["labels"]["funding"] == "our side pays" and mirror["labels"]["funding"] == "other side pays"
    assert pick["labels"]["fear_greed"] == "unknown" and pick["signal_type"] == "levels_support_reclaim"
    import pandas as pd

    from collectors import v2_feed

    async def funding(symbol, limit=120):
        return pd.DataFrame({"ts": [pd.Timestamp("2026-10-21")], "last_funding_rate": [-0.0002]})
    monkeypatch.setattr(v2_feed, "fetch_funding", funding)
    st2 = CryptoState(symbol="solusdt", base_asset="SOL")              # no funding on state (Delta-only mode)
    r.crypto_store._states["solusdt"] = st2
    await r._bracket_paper_job(datetime(2026, 10, 21, 12, 30))          # same hour: no scan
    await r._bracket_paper_job(datetime(2026, 10, 21, 13, 5))           # same last bar: no second trade
    book = json.loads((tmp_path / "book.json").read_text())
    sol = [t for t in book["open"] if t["symbol"] == "SOLUSDT"]
    assert [t["labels"]["funding"] for t in sol] == ["other side pays", "our side pays"]   # shorts pay longs
    assert sum(t["source"] == "levels" and t["symbol"] == "ETHUSDT" for t in book["open"]) == 2


async def test_the_api_returns_wallet_views_per_source_and_trade_labels(tmp_path):
    import json
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler import bracket_api
    a = advance(open_trade("lv:a", "ethusdt", 1, "levels_support_reclaim", "primary", "levels", T0, 100.0,
                           labels={"setup": "support reclaim"}), bars([(100, 101.5, 99.9, 101.2)] * 2))
    (tmp_path / "closed.jsonl").write_text(json.dumps(a) + "\n")
    (tmp_path / "book.json").write_text(json.dumps({"open": [], "started": T0.isoformat()}))
    app = web.Application()
    bracket_api.register(app, SimpleNamespace(_BRACKET_CLOSED=str(tmp_path / "closed.jsonl"),
                                              _BRACKET_OPEN=str(tmp_path / "book.json")))
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/bracket")).json()
    assert body["wallets"]["levels"]["taken"] == 1 and body["wallets"]["levels"]["margin"] == 500
    assert body["wallets"]["intraday"]["taken"] == 0
    assert body["recent"][0]["labels"] == {"setup": "support reclaim"}
