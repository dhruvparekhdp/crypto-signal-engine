"""Put the paper book's open swing trades onto the Delta DEMO account (fake money) so the demo mirrors paper from
now on. Entry at the current mark, stop = the paper trade's current stop, same target. Testnet only.

    PYTHONPATH=. python scripts/delta_testnet_adopt.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx


async def main() -> None:
    from config.overrides import apply
    from config.settings import settings
    from execution.delta_book import DeltaBook
    from execution.delta_india import USER_AGENT, DeltaIndia, binance_to_delta_symbol
    from scheduler.keys_page import _stored
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    apply(settings, await _stored())
    assert "testnet" in settings.delta_india_testnet_url, "testnet only"
    async with AsyncSessionFactory() as s:
        repo = Repository(s)
        cycle = await repo.get_running_cycle()
        rows = [r for r in await repo.get_open_positions(cycle.id) if r.trade_mode == "swing"]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
        client = DeltaIndia(settings.delta_india_testnet_api_key.get_secret_value(),
                            settings.delta_india_testnet_api_secret.get_secret_value(),
                            base_url=settings.delta_india_testnet_url, client=http)
        book = DeltaBook(client, mode="live", live_orders=True, risk_pct=settings.delta_testnet_risk_pct,
                         max_open=settings.delta_testnet_max_open, backstop_r=settings.delta_india_backstop_r,
                         shadow_path=Path("data/delta_testnet.jsonl"), state_path=Path("data/delta_testnet_state.json"))
        for r in rows:
            t = (await http.get(f"{settings.delta_india_testnet_url}/v2/tickers/{binance_to_delta_symbol(r.symbol)}")).json()
            mark = float((t.get("result") or {}).get("mark_price") or 0)
            side = 1 if r.side == "long" else -1
            if not mark or side * (mark - r.stop_price) <= 0:
                print(f"{r.symbol}: skipped (mark {mark} already beyond the stop {r.stop_price})")
                continue
            plan = await book.on_paper_open(r.symbol, r.side, mark, r.stop_price, r.target_price, r.signal_type)
            print(f"{r.symbol}: {'OPENED' if plan.ok else 'not opened'} {plan.size} contracts at ~{mark} stop {r.stop_price:.6g} "
                  f"target {r.target_price:.6g} ({plan.reason})")


if __name__ == "__main__":
    asyncio.run(main())
