"""Delta-hedged straddles, paper research (22 Oct 2026)."""
from datetime import datetime, timedelta

import pytest

from analysis import vol_book as vb

NOW = datetime(2026, 10, 22, 6, 0)


def row(kind, strike, exp, bid, ask, delta, mark=None, iv=0.4, u="BTC"):
    return {"symbol": f"{kind}-{u}-{strike}-{exp}", "mark_price": str(mark if mark is not None else (bid + ask) / 2),
            "quotes": {"best_bid": str(bid), "best_ask": str(ask), "mark_iv": str(iv)}, "greeks": {"delta": str(delta)}}


def chain():
    rows = []
    for exp in ("221026", "231026", "301026"):          # today 12:00 (6 h away), tomorrow (30 h), weekly (8.25 d)
        for k in (98, 100, 102):
            rows += [row("C", k, exp, 3.0, 3.2, 0.5), row("P", k, exp, 3.0, 3.2, -0.5)]
    rows.append(row("C", 101, "231026", 0.0, 9.0, 0.5))  # unquoted bid: never picked
    return vb.parse_chain(rows)


def test_it_picks_the_next_day_and_the_weekly_expiry_at_the_money():
    c = chain()["BTC"]
    assert vb.pick(c, 100.6, NOW, "daily") == ("2026-10-23T12:00:00", 100.0)
    assert vb.pick(c, 101.4, NOW, "weekly") == ("2026-10-30T12:00:00", 102.0)


def test_a_wide_straddle_spread_is_not_traded():
    c = vb.parse_chain([row("C", 100, "231026", 2.0, 3.0, 0.5), row("P", 100, "231026", 2.0, 3.0, -0.5)])["BTC"]
    assert vb.pick(c, 100, NOW, "daily") is None


def legs(cd=0.5, pd=-0.5, cb=3.0, ca=3.2, pb=3.0, pa=3.2):
    return {"C": {"symbol": "C", "bid": cb, "ask": ca, "mark": (cb + ca) / 2, "iv": 0.4, "delta": cd},
            "P": {"symbol": "P", "bid": pb, "ask": pa, "mark": (pb + pa) / 2, "iv": 0.4, "delta": pd}}


def test_a_sold_straddle_is_hedged_when_delta_drifts_and_settles_at_intrinsic():
    pos = vb.open_pos("BTC|daily|sell", "BTC", "daily", -1, "2026-10-23T12:00:00", 100.0, legs(), 100.0, NOW, 1000)
    assert pos["q"] == 10 and pos["entry_premium"] == 6.0 and pos["hedges"] == 0       # sold at the bids
    open_fee = 2 * min(0.0001 * 100 * 10, 0.10 * 3.0 * 10) * 1.18
    assert pos["fees"] == pytest.approx(open_fee)
    vb.step(pos, legs(cd=0.7, pd=-0.3), 104.0, NOW + timedelta(hours=1))   # short 10 x 0.4 delta = -4: buy 4 perps
    assert pos["hedge"] == pytest.approx(4.0) and pos["hedges"] == 1 and pos["delta_now"] == 0
    vb.step(pos, legs(cd=0.71, pd=-0.3), 104.0, NOW + timedelta(hours=2))  # 0.1 drift per 10 units: inside the band
    assert pos["hedges"] == 1
    vb.close(pos, None, 102.0, NOW + timedelta(hours=30))
    assert pos["exit"] == "settled" and pos["value_now"] == 2.0
    assert pos["opt_pnl"] == pytest.approx(40.0)                          # sold 6, worth 2 at expiry, 10 units
    assert pos["hedge_pnl"] == pytest.approx(4 * (102 - 104))
    hedge_fees = (4 * 104 + 4 * 102) * 0.0005 * 1.18
    assert pos["total_usd"] == pytest.approx(40 - 8 - open_fee - hedge_fees, abs=1e-3)
    assert pos["total_pct"] == pytest.approx(pos["total_usd"] / 10, abs=1e-3) and pos["realized_vol"] > 0


def test_a_bought_straddle_pays_the_ask_and_sells_at_the_bid():
    pos = vb.open_pos("BTC|daily|buy", "BTC", "daily", 1, "2026-10-23T12:00:00", 100.0, legs(), 100.0, NOW, 1000)
    assert pos["entry_premium"] == pytest.approx(6.4)
    vb.close(pos, legs(cb=5.0, ca=5.2, pb=1.0, pa=1.2), 104.0, NOW + timedelta(hours=29))
    assert pos["exit"] == "before expiry" and pos["opt_pnl"] == pytest.approx((6.0 - 6.4) * 10)
    assert pos["spread_paid"] == pytest.approx(10 * (0.1 + 0.1 + 0.1 + 0.1))


async def test_the_job_runs_both_books_and_rolls_the_daily_straddle(tmp_path, monkeypatch):
    import json

    from config.settings import settings
    from scheduler.runner import AppRunner
    monkeypatch.setattr(settings, "vol_paper_underlyings", "BTC")
    r = AppRunner()
    r._VOL_OPEN, r._VOL_CLOSED = str(tmp_path / "vol.json"), str(tmp_path / "vol.jsonl")
    rows = [row(k, s, e, 3.0, 3.2, d) for e in ("231026", "241026", "301026") for s in (100,)
            for k, d in (("C", 0.5), ("P", -0.5))]

    async def fake_chain():
        return rows, {"BTC": 100.0}
    r._vol_chain = fake_chain
    out = await r._vol_paper_job(NOW)
    book = json.loads((tmp_path / "vol.json").read_text())
    assert out == {"open": 4, "opened": 4, "closed": 0}
    assert {k: v["expiry"][:10] for k, v in book["open"].items()} == {
        "BTC|daily|sell": "2026-10-23", "BTC|daily|buy": "2026-10-23",
        "BTC|weekly|sell": "2026-10-30", "BTC|weekly|buy": "2026-10-30"}
    out = await r._vol_paper_job(datetime(2026, 10, 23, 11, 45))          # 15 min before the daily expiry
    book = json.loads((tmp_path / "vol.json").read_text())
    assert out == {"open": 4, "opened": 2, "closed": 2}
    assert book["open"]["BTC|daily|sell"]["expiry"][:10] == "2026-10-24"
    closed = [json.loads(ln) for ln in open(tmp_path / "vol.jsonl")]
    assert sorted(c["key"] for c in closed) == ["BTC|daily|buy", "BTC|daily|sell"]


async def test_the_api_shows_open_books_and_closed_results(tmp_path):
    import json
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from scheduler import vol_api
    pos = vb.open_pos("BTC|daily|sell", "BTC", "daily", -1, "2026-10-23T12:00:00", 100.0, legs(), 100.0, NOW, 1000)
    done = vb.close(dict(pos), None, 101.0, NOW + timedelta(hours=30))
    (tmp_path / "vol.json").write_text(json.dumps({"open": {pos["key"]: pos}, "started": NOW.isoformat()}))
    (tmp_path / "vol.jsonl").write_text(json.dumps(done) + "\n")
    app = web.Application()
    vol_api.register(app, SimpleNamespace(_VOL_OPEN=str(tmp_path / "vol.json"), _VOL_CLOSED=str(tmp_path / "vol.jsonl")))
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/vol")).json()
    assert body["open"][0]["key"] == "BTC|daily|sell" and body["summary"]["side:sell"]["n"] == 1
    assert body["summary"]["book:BTC|daily|sell"]["win_pct"] == 100.0 and body["recent"][0]["exit"] == "settled"
