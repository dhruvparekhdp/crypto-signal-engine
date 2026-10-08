"""One-shot: turn on Delta India shadow mode (no live orders). Run on the server.

  cd /home/ubuntu/crypto-signal-engine && PYTHONPATH=. .venv/bin/python scripts/enable_delta_shadow.py
  sudo systemctl restart crypto-engine
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.overrides import BY_KEY, apply, coerce
from config.settings import settings
from storage.database import AsyncSessionFactory
from storage.repository import Repository

WANTED = {
    "delta_india_mode": "shadow",
    "delta_india_live_orders": False,
    "delta_only_mode": True,
    "delta_india_data_enabled": True,
    "delta_india_max_open": 3,
    "delta_india_risk_pct": 0.01,
    "delta_india_usd_inr": 83.0,
}


async def main() -> None:
    values = {k: coerce(BY_KEY[k], v) for k, v in WANTED.items()}
    applied = apply(settings, values)
    print("applied_live", applied)
    print(
        "mode", settings.delta_india_mode,
        "live_orders", settings.delta_india_live_orders,
        "delta_only", settings.delta_only_mode,
    )
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        stored = await repo.get_app_settings()
        stored.update(values)
        await repo.save_app_settings(stored)
        again = await repo.get_app_settings()
    print(
        "db_mode", again.get("delta_india_mode"),
        "db_live_orders", again.get("delta_india_live_orders"),
        "db_delta_only", again.get("delta_only_mode"),
    )


if __name__ == "__main__":
    asyncio.run(main())
