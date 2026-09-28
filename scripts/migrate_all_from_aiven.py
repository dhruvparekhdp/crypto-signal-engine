"""
Comprehensive Database Migration Script: Aiven PostgreSQL -> AWS RDS PostgreSQL

Transfers all tables missing from the previous partial migration:
- move_attributions (Mover page)
- v2_shadow_signals (V2 Shadow page)
- market_briefings (World briefings)
- market_events (Calendar events)
- event_shadow_trades (Shadow books)
- signal_reviews (AI review audit trail)
- trade_events (Paper trade events)
- app_settings (Operational dynamic settings)
- match_records, match_snapshots, match_completions, player_stats (Tennis historical)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from storage.database import _make_url

AIVEN_URL = os.getenv(
    "AIVEN_DATABASE_URL",
    "postgres://avnadmin:***@pg-292c2a11-crypto-signals.i.aivencloud.com:22128/defaultdb?sslmode=require"
)

# Priority tables to migrate (Mover and V2 first, then others)
TABLES_TO_MIGRATE = [
    "move_attributions",
    "v2_shadow_signals",
    "market_briefings",
    "market_events",
    "event_shadow_trades",
    "signal_reviews",
    "trade_events",
    "app_settings",
    "player_stats",
    "match_completions",
    "match_records",
    "match_snapshots",
]


async def migrate_table(src_engine, tgt_engine, table_name: str, batch_size: int = 1000):
    print(f"\n📦 Processing table: {table_name}...")
    
    # 1. Check source count
    async with src_engine.connect() as src_conn:
        try:
            count_res = await src_conn.execute(text(f"SELECT count(*) FROM {table_name}"))
            total_rows = count_res.scalar() or 0
        except Exception as e:
            print(f"  ⚠️ Could not read {table_name} on source: {e}")
            return

    if total_rows == 0:
        print(f"  ℹ️ Table {table_name} has 0 rows on source, skipping.")
        return

    print(f"  Found {total_rows} rows on source.")

    # 2. Get columns
    async with src_engine.connect() as src_conn:
        col_res = await src_conn.execute(
            text(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table_name}' ORDER BY ordinal_position")
        )
        cols = [r[0] for r in col_res.fetchall()]

    col_names = ", ".join(f'"{c}"' for c in cols)
    col_params = ", ".join(f":{c}" for c in cols)

    # 3. Read in batches and insert into target
    offset = 0
    copied = 0
    while offset < total_rows:
        async with src_engine.connect() as src_conn:
            rows_res = await src_conn.execute(
                text(f"SELECT {col_names} FROM {table_name} ORDER BY 1 LIMIT {batch_size} OFFSET {offset}")
            )
            rows = [dict(row._mapping) for row in rows_res.fetchall()]

        if not rows:
            break

        insert_stmt = text(f"""
            INSERT INTO {table_name} ({col_names})
            VALUES ({col_params})
            ON CONFLICT DO NOTHING
        """)

        async with tgt_engine.begin() as tgt_conn:
            await tgt_conn.execute(insert_stmt, rows)

        copied += len(rows)
        offset += batch_size
        print(f"  -> Migrated {copied}/{total_rows} rows...")

    # 4. Sync sequence if auto-increment column exists
    async with tgt_engine.begin() as tgt_conn:
        try:
            await tgt_conn.execute(text(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table_name}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table_name}), 1)
                );
            """))
        except Exception:
            pass

    print(f"  ✅ Completed {table_name}: {copied} rows transferred.")


async def run_migration(target_url: str):
    print("=" * 60)
    print("🚀 MIGRATING MISSING TABLES: AIVEN -> AWS RDS")
    print("=" * 60)

    src_clean, src_args = _make_url(AIVEN_URL)
    tgt_clean, tgt_args = _make_url(target_url)

    src_engine = create_async_engine(src_clean, connect_args=src_args)
    tgt_engine = create_async_engine(tgt_clean, connect_args=tgt_args)

    try:
        # First ensure all schema tables exist on target
        from storage.database import Base, _migrate_columns
        async with tgt_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with tgt_engine.connect() as conn:
            await _migrate_columns(conn)

        for table in TABLES_TO_MIGRATE:
            await migrate_table(src_engine, tgt_engine, table)

        print("\n🎉 ALL MISSING TABLES MIGRATED SUCCESSFULLY!")
    finally:
        await src_engine.dispose()
        await tgt_engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        target = os.getenv("DATABASE_URL")
        if not target or "aivencloud" in target or "neon.tech" in target:
            print("Usage: python scripts/migrate_all_from_aiven.py <AWS_RDS_DATABASE_URL>")
            sys.exit(1)
    else:
        target = sys.argv[1]

    asyncio.run(run_migration(target))
