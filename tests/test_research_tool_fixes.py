"""Roadmap Q-10: research-tool findings R3 (stepped replay NameError) and R4 (target before stop on one bar)."""
import inspect


def test_r4_every_simulator_counts_the_stop_when_one_bar_touches_both():
    from analysis.simulator import replay_engine, stepped_replay
    import scripts.backtest_multi_year_pipeline as pipeline
    for mod in (replay_engine, stepped_replay, pipeline):
        src = inspect.getsource(mod)
        assert "if hit_tp and not hit_sl:" in src, mod.__name__
        assert "\n                if hit_tp:\n" not in src and "\n            if hit_tp:\n" not in src, mod.__name__


def test_r5_replay_never_invents_candles_unless_asked(tmp_path):
    from analysis.simulator.replay_engine import MarketReplayEngine
    eng = MarketReplayEngine(["NOSUCHUSDT"], window_years=0.01, lake_root=str(tmp_path))
    assert len(eng.load_candles("NOSUCHUSDT")) == 0                 # missing data: skipped, not synthesised
    eng.allow_synthetic = True
    assert len(eng.load_candles("NOSUCHUSDT")) > 0                  # only on purpose


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_r12_v2_run_now_says_so_when_the_backtest_is_off(monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiohttp.test_utils import make_mocked_request

    from config.settings import settings
    from scheduler import v2_pages
    monkeypatch.setattr(settings, "v2_backtest_enabled", False)
    monkeypatch.setattr(v2_pages, "_admin", AsyncMock(return_value=None))
    job = AsyncMock()
    _, run = v2_pages.backtest_api(SimpleNamespace(_v2_bt_state={}, _v2_backtest_job=job))
    resp = await run(make_mocked_request("POST", "/api/v2/backtest/run"))
    assert resp.status == 409 and "switched off" in json.loads(resp.text)["error"]
    job.assert_not_called()


def test_r8_outcome_replay_ignores_the_bar_that_contains_the_signal():
    from analysis.regime_gate import virtual_outcome
    h = 3_600_000
    # signal at 10:30 inside the 10:00 bar; that bar's low (before the signal) touched the stop
    t, high, low, close = [10 * h, 11 * h], [101, 104], [94, 99], [100, 103.5]
    out = virtual_outcome("long", 100, 95, 103, t, high, low, close, start_ms=10 * h + h // 2, hold_ms=48 * h,
                          now_ms=12 * h)
    assert out is not None and out[0] == "won"


def test_r3_stepped_replay_defines_what_its_trade_loop_reads():
    from analysis.simulator import stepped_replay
    src = inspect.getsource(stepped_replay)
    i = src.index("cycle_report_path.write_text(json.dumps(cycle_results")
    assert "cycle_results = run_cycle_simulation(" in src[:i]
    assert "sorted_trades = sorted(" in src[:i]
