"""
Database Restore Utility
Restores all 20 tables and records from backups/neon_backup_latest.json.gz (or .json) to a target PostgreSQL database (e.g. Aiven.io).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.database import Base, _make_url, _migrate_columns
from storage.models import (
    AdminAuth, CommoditySnapshot, CryptoSignalLog, CryptoSnapshot,
    CryptoWatchlistEntry, Match, MatchCompletion, MatchRecord, MatchResult,
    MatchSnapshot, NewsSentiment, OddsSnapshot, PaperCycle, PaperPosition,
    PaperTrade, PaperTradingConfig, PlayerStats, SignalLog, SlamPoint,
    StrategyConfig,
)

ALL_MODELS = [
    AdminAuth, StrategyConfig, PaperTradingConfig, CryptoWatchlistEntry,
    PaperCycle, PaperPosition, PaperTrade, CryptoSignalLog, NewsSentiment,
    Match, PlayerStats, MatchRecord, SlamPoint, MatchSnapshot,
    MatchCompletion, MatchResult, SignalLog, CommoditySnapshot, OddsSnapshot,
    CryptoSnapshot,
]


def create_db_engine(raw_url: str):
    url, connect_args = _make_url(raw_url)
    return create_async_engine(url, echo=False, pool_pre_ping=True, connect_args=connect_args)


async def sync_sequence(session, table_name: str, pk_col: str = "id") -> None:
    try:
        query = text(f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', '{pk_col}'),
                COALESCE((SELECT MAX({pk_col}) FROM {table_name}), 1)
            );
        """)
        await session.execute(query)
        await session.commit()
    except Exception:
        await session.rollback()


async def restore_database(backup_file: str, target_url: str) -> None:
    print("\n" + "=" * 60)
    print("🚀 STARTING DATABASE RESTORE TO TARGET POSTGRESQL")
    print("=" * 60)

    # 1. Load backup file
    print(f"\n[1/3] Loading backup file: {backup_file}...")
    if backup_file.endswith(".gz"):
        with gzip.open(backup_file, "rt", encoding="utf-8") as f:
            data = json.load(f)
    else:
        with open(backup_file, "r", encoding="utf-8") as f:
            data = json.load(f)

    total_records = sum(len(rows) for rows in data.values())
    print(f" Backup contains {total_records:,} records across {len(data)} tables.")

    # 2. Initialize target database schemas
    print(f"\n[2/3] Connecting to target and creating schemas...")
    engine = create_db_engine(target_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        await _migrate_columns(conn)

    print(" Target schema and migrations initialized.")

    # 3. Insert records table-by-table
    print(f"\n[3/3] Restoring records...")
    for model in ALL_MODELS:
        tbl = model.__tablename__
        rows = data.get(tbl, [])
        if not rows:
            continue

        try:
            async with Session() as session:
                # Convert ISO string dates back to naive datetime objects
                for r in rows:
                    for col in model.__table__.columns:
                        val = r.get(col.name)
                        if val is not None and "date" in str(col.type).lower():
                            if isinstance(val, str):
                                try:
                                    dt = datetime.fromisoformat(val)
                                    r[col.name] = dt.replace(tzinfo=None)
                                except Exception:
                                    pass

                chunk_size = 500
                for i in range(0, len(rows), chunk_size):
                    chunk = rows[i : i + chunk_size]
                    await session.execute(model.__table__.insert(), chunk)
                await session.commit()
                await sync_sequence(session, tbl)
                print(f"  ✓ {tbl:<24} Restored {len(rows):<6} rows")
        except Exception as e:
            print(f"  ❌ Error restoring {tbl}: {e}")

    await engine.dispose()
    print("\n🎉 Restore completed successfully!\n")


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Restore backup to PostgreSQL")
    parser.add_argument("--backup", default="backups/neon_backup_latest.json.gz",
                        help="Path to backup file (.json or .json.gz)")
    parser.add_argument("--target", default=os.getenv("AIVEN_DATABASE_URL", os.getenv("DATABASE_URL")),
                        help="Target PostgreSQL database URL")
    args = parser.parse_args()

    if not args.target:
        print("❌ Error: --target database URL is required!")
        sys.exit(1)

    asyncio.run(restore_database(args.backup, args.target))


if __name__ == "__main__":
    main()
