"""Candidates paper track (26 Oct 2026): live-style exits per strategy, a promotion gate, never copied to Delta."""
from datetime import datetime, timedelta

import pytest

from analysis import candidate_book as cb

T0 = datetime(2026, 10, 26, 10, 0)            # bars start on the hour, so minute 59 closes an hour
COST_R = cb.COST * 100 / 3                     # entry 100, 1R = 3


def bars(prices, start=T0):
    """prices: list of (o, h, l, c) or a single price per minute."""
    out = []
    for i, p in enumerate(prices):
        o, h, lo, c = p if isinstance(p, tuple) else (p, p, p, p)
        out.append({"timestamp": start + timedelta(minutes=i), "open": o, "high": h, "low": lo, "close": c})
    return out


def swing(track="swing", side=1):
    stop, tgt = (97.0, 109.0) if side > 0 else (103.0, 91.0)
    return cb.open_trade(track, 1, "ethusdt", side, 100.0, stop, tgt, T0 - timedelta(seconds=30), "swing_donchian", "4h",
                         label="taken")


def test_levels_that_make_no_sense_open_nothing():
    assert cb.open_trade("swing", 1, "x", 1, 100.0, 100.0, 109.0, T0) is None          # no stop distance
    assert cb.open_trade("swing", 1, "x", 1, 100.0, 97.0, 95.0, T0) is None            # target below a long


def test_plain_stop_and_target_in_r_after_fees():
    tr = cb.advance(swing(), bars([100, (100, 100.5, 96.5, 97.0)]))
    assert tr["exit"] == "stop" and tr["r_net"] == pytest.approx(-1 - COST_R, abs=1e-3)
    tr = cb.advance(swing(side=-1), bars([100, (100, 100.2, 90.5, 91.0)]))
    assert tr["exit"] == "target" and tr["r_net"] == pytest.approx(3 - COST_R, abs=1e-3)


def test_the_profit_lock_waits_for_an_hourly_close_like_live():
    path = [100, 106.3] + [(104, 104.2, 101.0, 103.5)] + [104] * 55 + [(103, 103.2, 101.5, 102.0)]   # index 58
    path = path[:59] + [(103, 103.2, 101.5, 102.0)]                                                   # minute 59
    tr = cb.advance(swing(), bars(path))
    # 2.1R reached -> ladder locks 1R - 0.25R buffer = 102.25; the 101 wick is ignored, the 11:00 close at 102 exits
    assert tr["exit"] == "lock" and tr["exit_price"] == pytest.approx(102.0)
    assert tr["r_net"] == pytest.approx(2 / 3 - COST_R, abs=1e-3)


def test_the_ratchet_runs_past_3r_where_the_live_exit_takes_the_target():
    path = [100, 108.6, (108.6, 109.2, 108.5, 109.1)] + [109.0] * 56 + [(109, 109.1, 106.9, 107.0)]
    live = cb.advance(swing(), bars(path))
    assert live["exit"] == "target" and live["exit_price"] == pytest.approx(109.0)
    rat = cb.advance(swing("swing_ratchet"), bars(path))
    # 3.07R: target out to 3.5R (110.5), stop up to 2.55R (107.65); the 11:00 close at 107 exits there
    assert rat["exit"] == "lock" and rat["target"] == pytest.approx(110.5) and rat["exit_price"] == pytest.approx(107.0)


def test_intraday_trades_close_at_the_24_hour_limit():
    tr = cb.open_trade("intraday_pick", 2, "xrpusdt", 1, 100.0, 98.0, 104.0, T0 - timedelta(seconds=30))
    tr = cb.advance(tr, bars([100.0] + [100.5] * (24 * 60 + 5)))          # enters at 100, 1R = 2
    assert tr["exit"] == "time" and tr["r_net"] == pytest.approx(0.25 - cb.COST * 100 / 2, abs=1e-3)


def _closed(track, r, side=1, week_day=T0):
    return {"track": track, "r_net": r, "side": side, "label": "taken", "entry_ts": week_day.isoformat()}


def test_the_gate_needs_time_size_profit_a_better_control_and_both_market_directions():
    up_week, down_week = datetime(2026, 10, 19, 12), datetime(2026, 10, 26, 12)          # W43 rising, W44 falling
    weekdir = cb.btc_week_dir({"2026-10-19": [80000, 80100], "2026-10-25": [80100, 82000],
                               "2026-10-26": [82000, 81000], "2026-11-01": [81000, 79000]})
    assert weekdir == {"2026-W43": 1, "2026-W44": -1}
    good = [_closed("intraday_pick", 0.3, week_day=up_week) for _ in range(60)] + \
           [_closed("intraday_pick", 0.1, week_day=down_week) for _ in range(60)]
    ctrl = [_closed("intraday_mirror", -0.4, week_day=up_week) for _ in range(120)]
    card = cb.scorecard(good + ctrl, [], weekdir, (T0 - timedelta(days=30)).isoformat(), T0)
    assert card["tracks"]["intraday_pick"]["status"].startswith("qualifies")
    assert card["tracks"]["intraday_mirror"]["status"] == "control"
    one_way = [_closed("intraday_pick", 0.6, week_day=down_week) for _ in range(120)]       # only a falling market
    card = cb.scorecard(one_way + ctrl, [], weekdir, (T0 - timedelta(days=30)).isoformat(), T0)
    t = card["tracks"]["intraday_pick"]
    assert t["status"] == "not qualifying"
    assert [c["ok"] for c in t["gate"] if "rising" in c["check"]] == [False]
    young = cb.scorecard(good + ctrl, [], weekdir, (T0 - timedelta(days=5)).isoformat(), T0)
    assert young["tracks"]["intraday_pick"]["status"] == "collecting"


async def test_the_job_routes_every_signal_to_its_tracks_and_waits_for_the_paper_decision(tmp_path, monkeypatch):
    import json

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from scheduler.runner import AppRunner
    from storage.models import Base, CryptoSignalLog
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", sm)
    log = dict(signal_type="confluence", trigger_description="x", confidence=0.7, current_price=100.0, target_price=103,
               stop_loss=98, edge_pct=0, stake_pct=0, timeframe="1h")
    now = datetime(2026, 10, 26, 12, 0)
    async with sm() as s:
        repo = Repository(s)
        for _ in range(7):
            await repo.log_crypto_signal(symbol="ethusdt", direction="long", **log)
        old = now - timedelta(minutes=20)
        rows = {1: dict(timestamp=old),
                2: dict(timestamp=old, trade_mode="swing", signal_type="swing_donchian", skip_reason="news_blackout",
                        stop_loss=97, target_price=109),
                3: dict(timestamp=old),                                                  # intraday pick
                4: dict(timestamp=old, candidate_role="mirror", mirror_of_log_id=3, direction="short",
                        stop_loss=102, target_price=97),
                5: dict(timestamp=old, candidate_role="mirror_shadow", direction="short", stop_loss=102,
                        target_price=97),
                6: dict(timestamp=old, candidate_role="mirror", mirror_of_log_id=5, stop_loss=98, target_price=103),
                7: dict(timestamp=now - timedelta(minutes=2))}                           # too young: next run
        for i, kw in rows.items():
            await s.execute(update(CryptoSignalLog).where(CryptoSignalLog.id == i).values(**kw))
        await s.commit()
    r = AppRunner()
    r._CAND_OPEN, r._CAND_CLOSED = str(tmp_path / "c.json"), str(tmp_path / "c.jsonl")
    (tmp_path / "c.json").write_text(json.dumps({"last_id": 1, "open": [], "started": now.isoformat(),
                                                 "parents": {}, "btc_days": {}}))

    async def no_bars(*a):
        return []
    r._signal_bars = no_bars
    out = await r._candidate_book_job(now)
    book = json.loads((tmp_path / "c.json").read_text())
    got = sorted((t["track"], t["side"], t["label"]) for t in book["open"])
    assert got == sorted([("swing", 1, "news_blackout"), ("swing_ratchet", 1, "news_blackout"),
                          ("swing_mirror", -1, "news_blackout"), ("intraday_pick", 1, "-"),
                          ("intraday_mirror", -1, "-"), ("counter_trend", -1, "-"), ("counter_trend_mirror", 1, "-")])
    assert book["last_id"] == 6 and out["new"] == 7
    mirror = next(t for t in book["open"] if t["track"] == "swing_mirror")
    assert mirror["stop_frac"] == pytest.approx(0.03) and mirror["rr"] == pytest.approx(3.0)


async def test_the_api_shows_each_strategy_with_its_gate(tmp_path):
    import json
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler import candidates_api
    tr = cb.advance(swing(), bars([100, (100, 100.5, 96.5, 97.0)]))
    (tmp_path / "c.jsonl").write_text(json.dumps(tr) + "\n")
    (tmp_path / "c.json").write_text(json.dumps({"open": [], "started": T0.isoformat(), "btc_days": {}}))
    app = web.Application()
    candidates_api.register(app, SimpleNamespace(_CAND_CLOSED=str(tmp_path / "c.jsonl"), _CAND_OPEN=str(tmp_path / "c.json")))
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/candidates")).json()
    t = body["tracks"]["swing"]
    assert t["closed"]["n"] == 1 and t["status"] == "collecting" and t["name"].startswith("Swing signals")
    assert t["labels"]["taken"]["n"] == 1 and any(c["check"] == "beats its control" for c in t["gate"])


async def test_2h_candidates_are_scored_in_paper_and_never_queued_for_a_trade(tmp_path, monkeypatch):
    # owner 28 Oct 2026: "add some strategy and trades in paper" - 2h passed the lab, so it is scored, not traded
    import json

    from datetime import UTC
    from unittest.mock import AsyncMock

    import numpy as np
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis import swing_book as sb
    from analysis.crypto_state import CryptoState
    from analysis.lab.data import INTERVAL_MS
    from config.settings import settings
    from scheduler.runner import AppRunner
    from storage.models import Base
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", sm)
    monkeypatch.setattr(settings, "swing_enabled", True)
    monkeypatch.setattr(settings, "paper_trading_enabled", True)
    monkeypatch.setattr(settings, "swing_registry", False)
    r = AppRunner()
    r._swing_seen = {"x": 0}
    r._save_swing_seen = lambda: None
    st = CryptoState(symbol="ethusdt", base_asset="ETH")
    st.current_price = 100.0
    r.crypto_store._states["ethusdt"] = st
    seen_tfs = []

    async def fake_fetch(client, sym, now_ms, tf):
        seen_tfs.append(tf)
        iv = INTERVAL_MS[tf]
        last = now_ms - iv - 60_000                          # a bar that closed a minute ago
        t = np.arange(last - 199 * iv, last + 1, iv)
        one = np.full(len(t), 100.0)
        return sb.Bars(sym, tf, t, one, one, one, one, one, one)

    def fake_eval(b, specs):
        assert any(tf == "2h" for tf, _, _ in specs)
        return sb.SwingSetup(b.symbol, "donchian", 1, int(b.t[-1]), 1.0, 100.0, b.interval) if b.interval == "2h" else None
    monkeypatch.setattr(sb, "fetch_bars", fake_fetch)
    monkeypatch.setattr(sb, "evaluate", fake_eval)
    r._regime_verdict = AsyncMock(return_value=None)
    monkeypatch.setattr("analysis.lab.runner.CRYPTO", ["ETHUSDT"])
    await r._swing_scan_job()
    assert "2h" in seen_tfs and "4h" in seen_tfs
    assert r._pending_swing == []                            # never reaches the paper wallet or Delta
    async with sm() as s:
        logs = await Repository(s).crypto_signals_between(1, 0, include_suppressed=True)
    assert [(x.timeframe, x.skip_reason, x.trade_mode) for x in logs] == [("2h", "paper_candidate_only", "swing")]

    now = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10)
    r._CAND_OPEN, r._CAND_CLOSED = str(tmp_path / "c.json"), str(tmp_path / "c.jsonl")
    (tmp_path / "c.json").write_text(json.dumps({"last_id": 0, "open": [], "started": now.isoformat(),
                                                 "parents": {}, "btc_days": {}}))

    async def no_bars(*a):
        return []
    r._signal_bars = no_bars
    await r._candidate_book_job(now)
    book = json.loads((tmp_path / "c.json").read_text())
    assert sorted((t["track"], t["side"]) for t in book["open"]) == [("swing_2h", 1), ("swing_2h_mirror", -1)]
