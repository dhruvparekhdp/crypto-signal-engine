"""Live move detector (owner 30 Oct 2026): alerts and paper only; the AI reads each move with the live news."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analysis import impulse as im


def bars(moves_pct, start=100.0, vol=10.0, last_vol=None):
    """16+ 1m bars: flat, then the last 15 move by moves_pct in total."""
    out, p, t = [], start, 1_793_000_000_000
    for i in range(20):
        nxt = p * (1 + moves_pct / 100 / 15) if i >= 5 else p
        v = (last_vol if (last_vol is not None and i >= 5) else vol)
        out.append((t + i * 60_000, p, max(p, nxt), min(p, nxt), nxt, v))
        p = nxt
    return out


BASE = (0.0025, 150.0)                     # normal 15-min move 0.25%, normal 15-min volume 150


def test_baseline_needs_a_day_of_bars():
    closes = [100 * (1 + 0.001 * ((-1) ** i)) for i in range(97)]
    sd, vm = im.baseline(closes, [5.0] * 96)
    assert 0.0019 < sd < 0.0021 and vm == 5.0
    assert im.baseline(closes[:20], [5.0] * 20) is None


def test_a_big_move_on_heavy_volume_fires_and_a_quiet_one_does_not():
    up = im.detect("btcusdt", bars(1.5, last_vol=40.0), BASE)          # 1.5% = 6x normal; volume 600 = 4x
    assert up and up.side == 1 and up.symbol == "BTCUSDT" and up.move_pct == pytest.approx(1.5, abs=0.02)
    assert up.sd_mult == pytest.approx(6.0, abs=0.05) and up.vol_mult == pytest.approx(4.0)
    assert im.detect("btcusdt", bars(-1.5, last_vol=40.0), BASE).side == -1
    assert im.detect("btcusdt", bars(0.8, last_vol=40.0), BASE) is None       # under the 1% floor
    assert im.detect("btcusdt", bars(1.5, last_vol=20.0), BASE) is None       # volume only 2x
    assert im.detect("btcusdt", bars(1.5, last_vol=40.0), (0.005, 150.0)) is None   # only 3x a volatile coin's normal


def test_levels_are_the_labs_best_exit():
    imp = im.Impulse("BTCUSDT", 1, 1.5, 6, 4, 101.5, 101.5, 100.0, 0)
    stop, tgt = im.levels(imp, 1)                       # 2 x (101.5 - 100) = 3.0 > 1.5% of 101.5
    assert stop == pytest.approx(98.5) and tgt == pytest.approx(101.5 + 9.0)
    stop, tgt = im.levels(imp, -1)                      # fade: range to the high is 0 -> the 1.5% floor
    assert stop == pytest.approx(101.5 * 1.015) and tgt == pytest.approx(101.5 - 3 * 101.5 * 0.015)


def test_the_ai_read_is_checked_and_turned_into_a_side():
    got = im.parse_ai({"summary": "PCE hot", "moves": [
        {"symbol": "btcusdt", "call": "Continue", "confidence": 0.8, "cause": "hot PCE", "cause_type": "macro_release"},
        {"symbol": "ETHUSDT", "call": "moon", "confidence": 3},
        {"symbol": "DOGEUSDT", "call": "reverse", "confidence": 0.9}]}, {"BTCUSDT", "ETHUSDT"})
    assert got["reads"]["BTCUSDT"]["call"] == "continue" and "DOGEUSDT" not in got["reads"]
    assert got["reads"]["ETHUSDT"] == {**got["reads"]["ETHUSDT"], "call": "unclear", "confidence": 1.0}
    assert im.ai_side(-1, got["reads"]["BTCUSDT"]) == -1
    assert im.ai_side(1, {"call": "reverse", "confidence": 0.6}) == -1
    assert im.ai_side(1, {"call": "reverse", "confidence": 0.4}) == 0 and im.ai_side(1, None) == 0
    assert im.parse_ai("not json", {"BTCUSDT"}) == {"summary": "", "reads": {}}


@pytest.mark.asyncio
async def test_a_move_gets_an_ai_read_an_alert_and_three_paper_tracks(tmp_path, monkeypatch):
    from collectors import llm_client
    from config.settings import settings
    from scheduler.runner import AppRunner
    monkeypatch.setattr(settings, "impulse_enabled", True)
    monkeypatch.setattr(settings, "impulse_alerts", True)
    r = AppRunner()
    r._IMPULSE_LOG = str(tmp_path / "imp.jsonl")
    r.notifier = SimpleNamespace(send_text=AsyncMock(return_value=True))
    r.crypto_store.get_symbols = AsyncMock(return_value=["btcusdt", "ethusdt"])
    r._impulse_context = AsyncMock(return_value={"now": "x", "headlines": ["US PCE hotter than expected"]})

    async def fake_klines(client, symbol, interval, limit):
        if interval == "15m":
            return [(i, 100, 100, 100, 100 * (1 + 0.00125 * ((-1) ** i)), 150.0) for i in range(98)]
        return bars(1.5 if symbol == "BTCUSDT" else 0.2, last_vol=40.0)
    r._impulse_klines = fake_klines
    monkeypatch.setattr(llm_client, "chain_for", lambda role: ["gemini"])
    ask = AsyncMock(return_value=SimpleNamespace(served_by="gemini", data={"summary": "hot PCE", "moves": [
        {"symbol": "BTCUSDT", "call": "reverse", "confidence": 0.7, "cause": "PCE", "cause_type": "macro_release"}]}))
    monkeypatch.setattr(llm_client, "ask_json", ask)
    out = await r._impulse_job()
    assert out == {"moves": 1} and ask.await_count == 1
    ev = json.loads((tmp_path / "imp.jsonl").read_text())
    assert ev["symbol"] == "BTCUSDT" and ev["ai"]["call"] == "reverse" and ev["breadth"]["coins"] == 2
    assert "BTC" in r.notifier.send_text.await_args.args[0] and "reverse" in r.notifier.send_text.await_args.args[0]
    assert (await r._impulse_job()) == {"moves": 0}                          # 4 h cooldown per coin

    from analysis import candidate_book as cb
    book = {"open": []}
    assert r._impulse_candidates(book, cb) == 3 and book["impulse_line"] == 1
    got = sorted((t["track"], t["side"]) for t in book["open"])
    assert got == [("impulse_ai", -1), ("impulse_fade", -1), ("impulse_follow", 1)]
    assert all(t["hold_s"] == 72 * 3600 and t["exit_kind"] == "fixed" for t in book["open"])
    assert r._impulse_candidates(book, cb) == 0                             # each line once


def test_the_detector_never_touches_live_orders():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("analysis/impulse.py").read_text())
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
            {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(str(x).startswith("execution") for x in names)
