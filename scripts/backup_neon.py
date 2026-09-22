"""
Neon Database Backup Utility
Exports all tables, rows, and schema to a local backup file (backups/neon_backup.json and .sql).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.database import _make_url
from storage.models import (
    AdminAuth, CommoditySnapshot, CryptoSignalLog, CryptoSnapshot,
    CryptoWatchlistEntry, NewsSentiment, PaperCycle, PaperPosition,
    PaperTrade, PaperTradingConfig, StrategyConfig,
)

ALL_MODELS = [
    AdminAuth, StrategyConfig, PaperTradingConfig, CryptoWatchlistEntry,
    PaperCycle, PaperPosition, PaperTrade, CryptoSignalLog, NewsSentiment,
    CommoditySnapshot, CryptoSnapshot,
]


# Columns that must never reach a backup file.
#
# This repository is public, and `backups/*.json.gz` is deliberately NOT
# gitignored — the point of the backup is that it is committed. A dump of
# admin_auth therefore publishes the password hash, its salt and a live
# session token to anyone who clones. The token alone is enough to act as the
# operator until someone changes the password.
#
# Redacted at the dump, not at commit time: a rule you have to remember to
# apply is a rule that gets forgotten exactly once, and once is enough.
REDACTED_COLUMNS = {
    "admin_auth": {"password_hash", "salt", "session_token"},
}


def default_json_serializer(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


async def backup_database(url: str, output_dir: str = "backups") -> None:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)

    print(f"\n[1/3] Connecting to database...")
    clean_url, connect_args = _make_url(url)
    engine = create_async_engine(clean_url, echo=False, pool_pre_ping=True, connect_args=connect_args)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with Session() as session:
            await session.execute(text("SELECT 1"))
        print(" Connected successfully.")
    except Exception as e:
        print(f"\n❌ Connection Failed: {type(e).__name__} - {e}\n")
        if "quota" in str(e).lower() or "InsufficientResources" in type(e).__name__:
            print("🚨 Neon has suspended compute because the free-tier quota was exceeded.")
            print("   Please unpause or upgrade the project in https://console.neon.tech for 5 minutes,")
            print("   then run this script again.")
        await engine.dispose()
        return

    print("\n[2/3] Dumping tables...")
    backup_data: dict[str, list[dict]] = {}
    total_rows = 0

    for model in ALL_MODELS:
        tbl = model.__tablename__
        try:
            async with Session() as session:
                res = await session.execute(select(model))
                rows = res.scalars().all()
                serialized = []
                redact = REDACTED_COLUMNS.get(tbl, frozenset())
                for r in rows:
                    d = {c.name: (None if c.name in redact else getattr(r, c.name))
                         for c in model.__table__.columns}
                    serialized.append(d)
                if redact and rows:
                    print(f"  · {tbl}: redacted {sorted(redact)}")
                backup_data[tbl] = serialized
                total_rows += len(serialized)
                print(f"  ✓ {tbl:<24} {len(serialized):<6} rows")
        except Exception as e:
            print(f"  ⚠️  Error reading {tbl}: {e}")
            backup_data[tbl] = []

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    json_file = path / f"neon_backup_{timestamp}.json"
    latest_json = path / "neon_backup_latest.json"

    print(f"\n[3/3] Writing backup files...")
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, default=default_json_serializer)

    with open(latest_json, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, indent=2, default=default_json_serializer)

    print(f"\n🎉 Backup completed successfully!")
    print(f" -> Total rows saved: {total_rows}")
    print(f" -> File: {json_file}")
    print(f" -> Latest: {latest_json}\n")

    await engine.dispose()


def main():
    load_dotenv()
    url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DATABASE_URL")
    if not url:
        print("Usage: python scripts/backup_neon.py <DATABASE_URL>")
        sys.exit(1)
    asyncio.run(backup_database(url))


if __name__ == "__main__":
    main()
