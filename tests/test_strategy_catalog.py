"""Strategy declarations: valid catalog, integration payload, version on signals and trades."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis import strategy_registry as reg
from analysis.paper_trading import ExitReason, FeeModel, Side, open_position
from analysis.strategy_catalog import (
    Declaration,
    strategies_payload,
    validate_catalog,
    validate_declaration,
    version_for,
)
from storage.database import _COLUMN_MIGRATIONS, Base
from storage.models import CryptoSignalLog, PaperPosition, PaperTrade
from storage.repository import Repository


def test_catalog_is_valid_and_versions_are_semver():
    assert validate_catalog() == []
    for name in ("donchian", "keltner_break", "vol_breakout", "ichimoku"):
        assert version_for(f"swing_{name}", "4h") == "1.0.0"
    assert version_for("confluence", "15m") == ""
    assert version_for("", "4h") == ""


def test_declaration_rules_reject_a_bad_version_status_or_default():
    base = dict(
        name="demo", version="1.0.0", status="research", description="A demo.",
        parameter_schema={"n": {"type": "integer", "default": 1}}, owner_notes="",
    )
    assert validate_declaration(Declaration(**base)) == []
    bad_version = Declaration(**{**base, "version": "1"})
    bad_status = Declaration(**{**base, "status": "paused"})
    assert any("version" in e for e in validate_declaration(bad_version))
    assert any("status" in e for e in validate_declaration(bad_status))
    bad_default = {"n": {"type": "integer", "default": True}}
    assert any("default" in e for e in validate_declaration(
        Declaration(**{**base, "parameter_schema": bad_default})))
    assert any("default" in e for e in validate_declaration(
        Declaration(**{**base, "parameter_schema": {"z": {"type": "number"}}})))


async def _db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_sync_stores_the_declaration_and_the_payload_feeds_strategies():
    sm = await _db()
    async with sm() as session:
        await reg.sync(session, [("4h", "vol_breakout", {"z": 3.0}), ("8h", "ichimoku", {})])
        rows = list((await session.execute(
            select(reg.StrategyRegistry).order_by(reg.StrategyRegistry.spec_key)
        )).scalars())
    assert rows[0].spec_key == "4h@vol_breakout"
    assert rows[0].version == "1.0.0"
    assert rows[0].lifecycle == "paper"
    assert rows[0].name == "vol_breakout"
    assert '"default": 2.0' in rows[0].parameter_schema
    payload = strategies_payload(rows)
    by_key = {item["spec_key"]: item for item in payload}
    vol = by_key["4h@vol_breakout"]
    assert vol["status"] == "paper"
    assert vol["version"] == "1.0.0"
    assert vol["parameter_schema"]["z"]["default"] == 2.0
    assert vol["owner_notes"]
    assert vol["description"]
    assert vol["operational_status"] == "incubating"
    assert by_key["8h@ichimoku"]["parameter_schema"] == {}
    empty = strategies_payload([])
    assert {item["name"] for item in empty} == {
        "donchian", "keltner_break", "vol_breakout", "ichimoku",
    }
    assert {item["status"] for item in empty} <= {"research", "paper", "retired"}


@pytest.mark.asyncio
async def test_signals_and_trades_record_the_strategy_version():
    sm = await _db()
    when = datetime.now(UTC)
    async with sm() as session:
        repo = Repository(session)
        swing_id = await repo.log_crypto_signal(
            symbol="solusdt", signal_type="swing_vol_breakout", direction="long",
            trigger_description="bar", confidence=0.8, current_price=100,
            target_price=110, stop_loss=95, edge_pct=0, stake_pct=0, timeframe="4h",
            trade_mode="swing",
        )
        other_id = await repo.log_crypto_signal(
            symbol="btcusdt", signal_type="confluence", direction="long",
            trigger_description="bar", confidence=0.5, current_price=100,
            target_price=101, stop_loss=99, edge_pct=0, stake_pct=0, timeframe="15m",
        )
        swing = await session.get(CryptoSignalLog, swing_id)
        other = await session.get(CryptoSignalLog, other_id)
        assert swing.strategy_version == "1.0.0"
        assert other.strategy_version == ""
        pos = open_position(
            "solusdt", Side.LONG, 100, 10, 2, FeeModel(), 0.2, 2.0, when,
            signal_type="swing_vol_breakout", timeframe="4h",
            stop_price=95, target_price=110,
        )
        assert pos.strategy_version == "1.0.0"
        session.add(repo._position_row(1, pos))
        repo._add_trade(1, type("Trade", (), {
            "position": pos, "exit_price": 110, "reason": ExitReason.TARGET,
            "gross_pnl": 1.0, "fees_paid": 0.1, "funding_paid": 0.0, "net_pnl": 0.9,
            "return_on_margin": 0.09,
            "wallet_after": 10.0, "entry_slippage_pct": 0.0, "hours_held": 1.0,
            "closed_at": when,
        })())
        await session.commit()
        stored = (await session.execute(select(PaperPosition))).scalar_one()
        closed = (await session.execute(select(PaperTrade))).scalar_one()
        assert stored.strategy_version == "1.0.0"
        assert closed.strategy_version == "1.0.0"


def test_column_migrations_add_the_declaration_and_version():
    text = "\n".join(_COLUMN_MIGRATIONS)
    for column in (
        "strategy_registry ADD COLUMN IF NOT EXISTS version",
        "strategy_registry ADD COLUMN IF NOT EXISTS lifecycle",
        "crypto_signal_log ADD COLUMN IF NOT EXISTS strategy_version",
        "paper_trades ADD COLUMN IF NOT EXISTS strategy_version",
        "paper_positions ADD COLUMN IF NOT EXISTS strategy_version",
    ):
        assert column in text
