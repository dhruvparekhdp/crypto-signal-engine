"""
Position size per trade (floor/ceiling of wallet, scaled by confidence) was
hardcoded in SizingConfig()'s defaults — 16.7% to 50% — with no way to see
or change it short of editing the code. 27 Sep: the owner's own trades
showed why that was invisible — a SOL trade at 75% confidence used ~33% of
the wallet, already the biggest share the old defaults gave any signal,
but the ABSOLUTE rupee amounts still read as "low" because the wallet
itself (₹1,972 of a ₹3,000 start) is small. Made both ends of the range a
saved setting, defaulting to a flat 25%.
"""
import unittest


class TestConfigForCycleWiring(unittest.TestCase):
    """config_for_cycle() must read the row's saved floor/ceiling, not the
    SizingConfig dataclass's own hardcoded defaults."""

    def test_the_saved_percentages_reach_sizingconfig(self):
        from analysis.paper_cycle import config_for_cycle
        from storage.models import PaperTradingConfig
        row = PaperTradingConfig(
            starting_wallet=3000.0, target_wallet=20000.0, leverage=10.0,
            stop_pct_of_margin=0.20, reward_risk=2.0, min_confidence=0.70,
            scaled_sizing=True, sizing_floor_pct=0.10, sizing_ceiling_pct=0.40)
        cfg = config_for_cycle(row)
        self.assertIsNotNone(cfg.sizing)
        self.assertEqual(cfg.sizing.floor_margin_pct, 0.10)
        self.assertEqual(cfg.sizing.ceiling_margin_pct, 0.40)

    def test_equal_floor_and_ceiling_is_flat_sizing_regardless_of_confidence(self):
        from analysis.paper_cycle import config_for_cycle
        from storage.models import PaperTradingConfig
        row = PaperTradingConfig(
            starting_wallet=1000.0, target_wallet=20000.0, leverage=10.0,
            stop_pct_of_margin=0.20, reward_risk=2.0, min_confidence=0.70,
            scaled_sizing=True, sizing_floor_pct=0.25, sizing_ceiling_pct=0.25)
        cfg = config_for_cycle(row)
        weak = cfg.sizing.margin_for(1000.0, confidence=0.65)
        strong = cfg.sizing.margin_for(1000.0, confidence=0.85)
        self.assertEqual(weak, strong)
        self.assertEqual(weak, 250.0)

    def test_scaled_sizing_off_ignores_the_percentages_entirely(self):
        from analysis.paper_cycle import config_for_cycle
        from storage.models import PaperTradingConfig
        row = PaperTradingConfig(
            starting_wallet=1000.0, target_wallet=20000.0, leverage=10.0,
            stop_pct_of_margin=0.20, reward_risk=2.0, min_confidence=0.70,
            scaled_sizing=False, sizing_floor_pct=0.25, sizing_ceiling_pct=0.25)
        cfg = config_for_cycle(row)
        self.assertIsNone(cfg.sizing)


class TestMigrationIsWellFormed(unittest.IsolatedAsyncioTestCase):
    """
    _COLUMN_MIGRATIONS is a module-level list specifically so a test can
    read the real strings the app runs, not scrape function source text for
    a substring that may not even be one Python string once concatenated
    literals and comments are accounted for (the bug the first version of
    this test had). SQLite has never supported "ADD COLUMN IF NOT EXISTS"
    at all — no version does — so PostgreSQL syntax can only be asserted
    against a live Postgres. What every environment CAN check: the exact
    statement is present, and running the whole migration on SQLite still
    only ever hits the documented try/rollback, never raises.
    """

    def test_both_alter_statements_are_present_and_well_formed(self):
        from storage.database import _COLUMN_MIGRATIONS
        for stmt in (
            "ALTER TABLE paper_trading_config ADD COLUMN IF NOT EXISTS "
            "sizing_floor_pct FLOAT DEFAULT 0.25",
            "ALTER TABLE paper_trading_config ADD COLUMN IF NOT EXISTS "
            "sizing_ceiling_pct FLOAT DEFAULT 0.25",
        ):
            with self.subTest(stmt=stmt):
                self.assertIn(stmt, _COLUMN_MIGRATIONS)

    async def test_running_the_migration_on_sqlite_never_raises(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from storage.database import _migrate_columns
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(
                    "CREATE TABLE paper_trading_config (id INTEGER PRIMARY KEY)"))
                await conn.commit()
                await _migrate_columns(conn)   # must not raise
        finally:
            await engine.dispose()


class TestApiRoundTrip(unittest.IsolatedAsyncioTestCase):
    """
    Exercises the real GET/POST handlers, against a throwaway engine built
    from the ORM models directly (create_all, not the ALTER-based startup
    migration) rather than the process-wide storage.database.engine — that
    singleton binds to whatever DATABASE_URL was ambient the first time
    ANYTHING in the whole test session imported storage.models (Base lives
    there), which in a full run is long before this test gets to set its
    own. Patching storage.database.AsyncSessionFactory works because both
    handlers re-import it fresh, inside the function, on every call.
    """

    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import storage.models  # noqa: F401  (registers every table on Base)
        from storage.database import Base
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.patcher = __import__("unittest.mock", fromlist=["patch"]).patch(
            "storage.database.AsyncSessionFactory", maker)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()

    async def test_defaults_and_a_save_both_reach_the_paper_config_api(self):
        import json
        from unittest.mock import AsyncMock, patch

        from aiohttp.test_utils import make_mocked_request

        import scheduler.health as health

        resp = await health._api_paper_config_get(None, make_mocked_request("GET", "/"))
        get_body = json.loads(resp.text)
        self.assertEqual(get_body["sizing_floor_pct"], 0.25)
        self.assertEqual(get_body["sizing_ceiling_pct"], 0.25)

        with patch.object(health, "_verify_admin_session", AsyncMock(return_value=True)):
            post = make_mocked_request("POST", "/api/paper/config")
            post.json = AsyncMock(return_value={"sizing_floor_pct": 0.15,
                                                "sizing_ceiling_pct": 0.45})
            resp = await health._api_paper_config_post(None, post)
            self.assertEqual(resp.status, 200)

        resp = await health._api_paper_config_get(None, make_mocked_request("GET", "/"))
        saved = json.loads(resp.text)
        self.assertEqual(saved["sizing_floor_pct"], 0.15)
        self.assertEqual(saved["sizing_ceiling_pct"], 0.45)


if __name__ == "__main__":
    unittest.main()
