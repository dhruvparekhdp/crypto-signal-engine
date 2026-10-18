"""Paper on the live Delta book's rules (owner, 18 Oct 2026: "make paper cycle setup as same as delta live from leverage
to all the options... remove other blocking setting keep basic daily loss and one direction... no floor").

The live book only acts when paper opens a trade, so every paper-only blocker also blocked live: the first signal after
live started (SUI, 17 Oct 20:01 UTC) was skipped by paper's side-risk cap while live had nothing open."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis import swing_book as sb
from analysis.crypto_state import CryptoState
from config.settings import settings
from storage.models import Base

PRICES = {"XRPUSDT": 0.6, "DOGEUSDT": 0.2, "SOLUSDT": 150.0, "BTCUSDT": 60000.0, "XAUTUSDT": 4000.0, "LINKUSDT": 35.0}


@pytest.fixture(autouse=True)
def _live_rules(monkeypatch):
    for k, v in {"paper_mirrors_live": True, "delta_india_risk_pct": 0.03, "delta_india_max_open": 3,
                 "delta_india_daily_loss_pct": 0.10, "delta_india_min_leverage": 5, "delta_india_usd_inr": 85.0,
                 "delta_india_round_up_risk_pct": 0.05,
                 "delta_live_coins": "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,SUIUSDT",
                 "swing_max_same_side": 0}.items():
        monkeypatch.setattr(settings, k, v)


async def run(signals, seed=None, **patches):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    if seed is not None:
        await seed(sm)
    from scheduler.runner import AppRunner
    runner = AppRunner()
    runner.notifier = AsyncMock()
    runner._mirror_delta_india = AsyncMock()                  # no Delta calls from a test
    with patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.settings.session_filter_enabled", False), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        for k, v in patches.items():
            patch(f"scheduler.runner.settings.{k}", v).start()
        try:
            for sym, px in PRICES.items():
                st = CryptoState(symbol=sym.lower(), base_asset=sym[:-4])
                st.current_price, st.atr_14 = px, px * 0.005
                runner.crypto_store._states[sym.lower()] = st
            now = datetime.now(UTC)
            from analysis.regime_gate import Verdict
            for sym, strat, side in signals:
                px = PRICES[sym]
                s_ = sb.to_signal(sb.SwingSetup(sym, strat, side, 0, px * 0.015, px), px, now)   # stop 4.5% away
                s_.regime = Verdict(0.2, 15.0, [])
                runner._pending_swing.append(s_)
            await runner._paper_trading_job()
        finally:
            patch.stopall()
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            cycle = await repo.get_running_cycle()
            rows = await repo.get_open_positions(cycle.id)
            logs = await repo.crypto_signals_between(1, 0)
    return rows, {l.symbol: l.skip_reason for l in logs}, runner


@pytest.mark.asyncio
async def test_a_paper_trade_is_sized_exactly_like_the_live_order():
    rows, skips, _ = await run([("XRPUSDT", "donchian", +1), ("SOLUSDT", "donchian", +1)])
    assert [r.symbol for r in rows] == ["xrpusdt"]
    xrp = rows[0]
    wallet = 3000.0
    risk_inr = xrp.coin_qty * abs(xrp.entry_price - xrp.initial_stop_price) * xrp.usdt_inr
    assert risk_inr == pytest.approx(wallet * 0.03, rel=0.05)          # live risk: fixed 3% (not adaptive, not half)
    assert xrp.usdt_inr == 85.0                                        # Delta's rupee rate, not paper_usdt_inr (102)
    assert xrp.leverage == 10                                          # 4.5% stop -> the live rule's 10x
    assert xrp.coin_qty == int(xrp.coin_qty)                           # whole XRP contracts (1 XRP each)
    assert skips["solusdt"] == "below_one_contract"                     # 1 SOL contract is too big - live skips it too


@pytest.mark.asyncio
async def test_only_live_coins_take_a_live_slot():
    rows, skips, _ = await run([("XRPUSDT", "donchian", +1), ("DOGEUSDT", "donchian", +1),
                                ("XAUTUSDT", "donchian", +1)], delta_india_max_open=1)
    opened = sorted(r.symbol for r in rows)
    assert "xautusdt" in opened                                        # gold is paper-only: never takes a live slot
    live_opened = [s for s in opened if s != "xautusdt"]
    assert len(live_opened) == 1
    other = ({"xrpusdt", "dogeusdt"} - set(live_opened)).pop()
    assert skips[other] == "book_full_like_live"


@pytest.mark.asyncio
async def test_side_cap_and_registry_pause_no_longer_block():
    from storage.models import StrategyRegistry

    async def paused(sm):
        async with sm() as s:
            s.add(StrategyRegistry(spec_key="4h@donchian", digest="x", status="paused"))
            await s.commit()
    rows, skips, _ = await run([("XRPUSDT", "donchian", -1)], seed=paused, swing_max_side_risk_pct=0.001)
    assert [r.symbol for r in rows] == ["xrpusdt"] and not skips.get("xrpusdt")


def _closed(pnl, minutes_ago, now):
    return SimpleNamespace(symbol="xrpusdt", closed_at=(now - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
                           net_pnl=pnl, side="long")


def test_only_the_daily_loss_limit_stays():
    from scheduler.runner import AppRunner
    r = AppRunner.__new__(AppRunner)
    now = datetime.now(UTC).replace(hour=12)
    streak = [_closed(-30, 50, now), _closed(-30, 40, now), _closed(-30, 30, now)]      # 3 losses, Rs90 of Rs3,000
    assert r._swing_account_guard(streak, now, 2910.0) == "losing_streak_pause"         # the old paper rule
    assert r._swing_account_guard(streak, now, 2910.0, daily_loss_pct=10, streak_pause=False) is None
    bad_day = [_closed(-320, 30, now)]                                                  # 10.7% down today
    assert r._swing_account_guard(bad_day, now, 2680.0, daily_loss_pct=10, streak_pause=False) == "daily_loss_limit"


def test_a_locked_stop_is_not_risk():
    from scheduler.runner import open_risk_frac
    short = SimpleNamespace(side="short", entry_price=100.0, stop_price=104.0)
    assert open_risk_frac(short) == pytest.approx(0.04)
    short.stop_price = 99.0                                             # locked +0.25R below a short's entry
    assert open_risk_frac(short) == 0.0
    long_ = SimpleNamespace(side=SimpleNamespace(value="long"), entry_price=100.0, stop_price=101.0)
    assert open_risk_frac(long_) == 0.0


def test_paper_uses_the_rate_delta_reported():
    from scheduler.runner import AppRunner
    r = AppRunner.__new__(AppRunner)
    assert r._delta_usd_inr() == 85.0                                  # nothing read yet: the saved fallback
    r._delta_rate = 85.37
    assert r._delta_usd_inr() == 85.37
    r._delta_rate = 1.0                                                 # nonsense is ignored
    assert r._delta_usd_inr() == 85.0


@pytest.mark.asyncio
async def test_live_risk_is_a_share_of_the_whole_wallet(tmp_path):
    """Sizing on the available balance shrank the 2nd and 3rd live trade by the margin the 1st had locked; paper sizes
    on equity (cash + margin), so live now does too. Margin still has to fit in what is available."""
    from tests.test_delta_ladder import FakeDelta, _book

    class Wallet(FakeDelta):
        async def wallet_inr(self):
            return 300.0, 85.0                    # Rs200 of Rs500 already held as margin by an open trade

        async def wallet_total_inr(self):
            return 500.0
    book = _book(tmp_path, Wallet())
    plan = await book.plan("xrpusdt", "short", 1.40, 1.47, 1.19)
    assert plan.ok
    assert plan.risk_inr == pytest.approx(500 * 0.03, abs=0.07 * 85)  # 3% of Rs500, to one whole contract
    assert book.rate_seen and book.usd_inr == 85.0


def test_a_busted_cycle_is_announced_plainly():
    """No floor on paper: it trades until the wallet cannot fund a trade, then says so (owner 18 Oct)."""
    from notifications.crypto_formatter import format_cycle_end
    summary = {"wallet": 41.0, "trades": 60, "win_rate_pct": 30, "gross_pnl": -4800.0, "trading_fees": 120.0,
               "funding_paid": 39.0, "net_pnl": -4959.0, "longest_losing_streak": 9}
    text = format_cycle_end(SimpleNamespace(id=2, starting_wallet=5000.0), "busted", summary)
    assert "BUSTED THE PAPER CYCLE" in text and "cycle #2" in text


@pytest.mark.asyncio
async def test_the_five_minute_reconcile_learns_deltas_rupee_rate(tmp_path):
    from tests.test_delta_ladder import FakeDelta, _book

    class Rate(FakeDelta):
        async def wallet_inr(self):
            return 500.0, 85.37
    book = _book(tmp_path, Rate())
    await book.reconcile()
    assert book.rate_seen and book.usd_inr == 85.37


@pytest.mark.asyncio
async def test_old_paper_trades_never_block_live(tmp_path, monkeypatch):
    """With real money on, the live book's own open positions are the slots. Paper held 5 trades from before live
    started (18 Oct 2026) while live held none - those must not stop the next live trade."""
    import json as _json

    from execution import delta_book
    from scheduler.runner import AppRunner
    state = tmp_path / "delta_state.json"
    state.write_text(_json.dumps({"positions": {"SUIUSD": {}}}))
    monkeypatch.setattr(delta_book, "STATE_PATH", state)
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_india_live_orders", True)
    legacy = [SimpleNamespace(trade_mode="swing", symbol=s) for s in ("ethusdt", "linkusdt", "bnbusdt", "solusdt", "xrpusdt")]
    coins = {"ethusdt", "linkusdt", "bnbusdt", "solusdt", "xrpusdt", "suiusdt"}
    assert AppRunner._live_slots_used(legacy, coins) == 1                # live's own book: 1 of 3 used
    monkeypatch.setattr(settings, "delta_india_live_orders", False)
    assert AppRunner._live_slots_used(legacy, coins) == 5                # no real money: paper counts its own


@pytest.mark.asyncio
async def test_paper_rounds_up_to_one_contract_like_live():
    """LINK 35, 4.5% stop, Rs3,000: 3% buys 0.65 of a contract; one contract risks ~Rs134 (4.5%) <= 5% -> one."""
    rows, skips, _ = await run([("LINKUSDT", "donchian", +1)])
    assert [r.symbol for r in rows] == ["linkusdt"] and rows[0].coin_qty == 1.0
    risk = rows[0].coin_qty * abs(rows[0].entry_price - rows[0].initial_stop_price) * rows[0].usdt_inr
    assert 90 < risk <= 150
    rows, skips, _ = await run([("LINKUSDT", "donchian", +1)], delta_india_round_up_risk_pct=0.0)
    assert rows == [] and skips["linkusdt"] == "below_one_contract"


def test_tia_and_gold_are_live_coins_now():
    from config.settings import Settings
    coins = Settings.model_fields["delta_live_coins"].default
    assert "TIAUSDT" in coins and "XAUTUSDT" in coins


@pytest.mark.asyncio
async def test_paper_rounds_down_to_whole_contracts_like_live():
    """XRP 0.6, 4.5% stop: 3% of Rs3,000 buys 37.7 contracts; live sends 37, so paper holds 37 (not 38)."""
    rows, _, _ = await run([("XRPUSDT", "donchian", +1)])
    stop_frac = abs(rows[0].entry_price - rows[0].initial_stop_price) / rows[0].entry_price
    from analysis.paper_cycle import stop_out_costs
    raw = 3000 * 0.03 / stop_frac / (rows[0].usdt_inr * rows[0].entry_price)      # live: stop distance only
    assert rows[0].coin_qty == int(raw) and raw - int(raw) > 0.05                    # rounded DOWN, not to nearest
    _ = stop_out_costs
