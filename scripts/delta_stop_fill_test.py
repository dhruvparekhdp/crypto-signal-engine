"""TESTNET only (fake money) - run on the server: PYTHONPATH=. .venv/bin/python scripts/delta_stop_fill_test.py

TESTNET only: prove the exchange's own stop fills and reconcile notices it (the path a crash-day would take).
Opens a small ADAUSD short through DeltaBook, moves the exchange stop to just above the market, waits for Delta
to trigger it, then runs reconcile. Always cleans up."""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

import httpx

SYM, DSYM = "adausdt", "ADAUSD"


async def main() -> int:
    from config.overrides import apply
    from config.settings import settings
    from execution.delta_book import DeltaBook
    from execution.delta_india import USER_AGENT, DeltaIndia
    from scheduler.keys_page import _stored
    apply(settings, await _stored())
    base = settings.delta_india_testnet_url
    assert "testnet" in base, base
    k, s = settings.delta_india_testnet_api_key, settings.delta_india_testnet_api_secret
    tmp = Path(tempfile.mkdtemp(prefix="stophit_"))
    ok = []

    def step(n, c, d=""):
        ok.append(bool(c))
        print(f"[{'PASS' if c else 'FAIL'}] {n} {d}", flush=True)

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
        c = DeltaIndia(k.get_secret_value(), s.get_secret_value(), base_url=base, client=http)
        book = DeltaBook(c, mode="live", live_orders=True, risk_pct=0.005, max_open=1, usd_inr=86.0,
                         shadow_path=tmp / "log.jsonl", state_path=tmp / "state.json", backstop_r=0.5)
        p = await c.product(DSYM)
        try:
            pre = await c.position_size(p.id)
            if pre:
                print("ADAUSD already has a position on the demo account; refusing", pre)
                return 2

            async def mark():
                t = (await http.get(f"{base}/v2/tickers/{DSYM}")).json().get("result") or {}
                return float(t.get("mark_price") or 0)
            px = await mark()
            plan = await book.on_paper_open(SYM, "short", px, px * 1.05, px * 0.85, "stop_hit_test")
            step("open short with stop + target", plan.ok, f"{plan.size} contracts @ {px} reason={plan.reason}")
            if not plan.ok:
                return 1
            st = book._load_state()["positions"][DSYM]
            tick = p.tick_size or 0.0001
            # walk the exchange stop down to 2 ticks above the market until Delta accepts it, then wait for a trigger
            fired = False
            t0 = time.time()
            while time.time() - t0 < 900:
                m = await mark()
                want = round((m + 2 * tick) / tick) * tick
                orders = await c.open_orders(p.id)
                sl = [o for o in orders if o.get("stop_order_type") == "stop_loss_order"]
                if sl and abs(float(sl[0].get("stop_price") or 0) - want) > tick / 2:
                    try:
                        await c.edit_stop(sl[0]["id"], p.id, int(plan.size), want)
                    except Exception as e:  # noqa: BLE001
                        print("  edit refused (fine if immediate):", str(e)[:120])
                await asyncio.sleep(5)
                if await c.position_size(p.id) == 0:
                    fired = True
                    break
            step("exchange stop filled by Delta itself", fired, f"after {time.time() - t0:.0f}s")
            left_before = await c.open_orders(p.id)
            ev = await book.reconcile()
            kinds = [e.get("event") for e in ev]
            step("reconcile reports closed_on_exchange", "closed_on_exchange" in kinds, f"{ev}")
            await asyncio.sleep(1.5)
            left = await c.open_orders(p.id)
            step("leftover target order cancelled", not left, f"before reconcile {len(left_before)}, after {len(left)}")
            step("our record removed", DSYM not in (book._load_state().get("positions") or {}))
            _ = st
        finally:
            try:
                size = await c.position_size(p.id)
                if size:
                    await c.place_market(p.id, "buy" if size < 0 else "sell", abs(size), reduce_only=True)
                await c.cancel_all(p.id)
            except Exception as e:  # noqa: BLE001
                print("cleanup warning", str(e)[:120])
    print(f"{sum(ok)}/{len(ok)} passed")
    import json as _json
    from datetime import UTC, datetime
    Path("data").mkdir(exist_ok=True)
    Path("data/delta_stop_test.json").write_text(_json.dumps(
        {"passed": bool(ok) and all(ok), "steps": len(ok), "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")}))
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
