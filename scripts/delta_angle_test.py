"""Full-angle Delta India checks (shadow / data / auth). Run on the server."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> None:
    import httpx

    from analysis.swing_book import fetch_bars
    from collectors.delta_market import DeltaMarket
    from config.settings import settings
    from execution.delta_india import DeltaIndia

    print("=== SETTINGS ===")
    print(
        "mode", settings.delta_india_mode,
        "live_orders", settings.delta_india_live_orders,
        "delta_only", settings.delta_only_mode,
        "data", settings.delta_india_data_enabled,
        "base", settings.delta_india_base_url,
    )

    print("=== MARKS ===")
    m = DeltaMarket()
    px = await m.fetch_mark_prices()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"):
        print(s, px.get(s))
    print("n_marks", len(px))

    print("=== 1m CANDLES ===")
    for s in ("BTCUSDT", "SOLUSDT", "AVAXUSDT"):
        bars = await m.fetch_candles(s, "1m", limit=50)
        print(s, "n", len(bars), "last", bars[-1]["close"] if bars else None, "err", m.last_error)

    print("=== SWING 4h/8h ===")
    now = int(time.time() * 1000)
    async with httpx.AsyncClient() as c:
        for sym in ("BTCUSDT", "SOLUSDT", "AVAXUSDT", "LINKUSDT"):
            for tf in ("4h", "8h"):
                try:
                    b = await fetch_bars(c, sym, now, tf=tf, limit=300)
                    print(sym, tf, "bars", None if b is None else len(b.c))
                except Exception as e:  # noqa: BLE001
                    print(sym, tf, "ERR", type(e).__name__, str(e)[:140])

    print("=== AUTH / WALLET ===")
    key = settings.delta_india_api_key.get_secret_value() if settings.delta_india_api_key else ""
    sec = settings.delta_india_api_secret.get_secret_value() if settings.delta_india_api_secret else ""
    async with httpx.AsyncClient(timeout=20) as c:
        client = DeltaIndia(key, sec, base_url=settings.delta_india_base_url, client=c)
        rows = await client.balances()
        print("wallet_rows", len(rows), "inr_available", await client.inr_available())
        for row in rows[:10]:
            if not isinstance(row, dict):
                continue
            print(json.dumps({
                "asset": row.get("asset_symbol") or row.get("currency") or row.get("symbol"),
                "available": row.get("available_balance") or row.get("available_balance_inr") or row.get("balance"),
            }))
        for sym in ("XRPUSD", "DOGEUSD", "SOLUSD"):
            try:
                p = await client.product(sym)
                print("product", sym, "id", getattr(p, "id", None), "ok", p is not None)
            except Exception as e:  # noqa: BLE001
                print("product", sym, "ERR", str(e)[:120])

    shadow = Path("data/delta_shadow.jsonl")
    print("=== SHADOW FILE ===", shadow, "exists", shadow.exists())
    if shadow.exists():
        lines = shadow.read_text().splitlines()[-5:]
        for line in lines:
            d = json.loads(line)
            print(
                d.get("binance_symbol"), d.get("delta_symbol"),
                "size", d.get("size"), "placed", d.get("placed"),
                "ok", d.get("ok"), d.get("reason") or d.get("note"),
            )


if __name__ == "__main__":
    asyncio.run(main())
