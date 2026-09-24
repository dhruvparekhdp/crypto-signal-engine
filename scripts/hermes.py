"""
Hermes — run this on the machine with the model. It feeds the engine.

    python scripts/hermes.py --once              # one pass, see what happens
    python scripts/hermes.py --once --dry-run    # fetch and score, send nothing
    python scripts/hermes.py                     # loop every 15 minutes

Needs two things in the environment:

    ENGINE_URL           http://52.62.37.4:8080   (or the Tailscale name)
    SENTIMENT_INGEST_TOKEN                        same value as on the engine
    OLLAMA_BASE_URL      http://localhost:11434   (this box, so localhost)

Everything it reads is free RSS. Nothing here needs an API key, a signup or a
quota — including the Federal Reserve's own press-release feed, which is where
a rate decision appears first and is the single most valuable line in the
list.

Why a separate process at all: the engine is on a rented box and the model is
on this one. Hermes sits with the model, scores locally, and pushes the result
over the tunnel, because the engine cannot dial into a home network.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from collectors.hermes import HIGH_IMPACT, fetch_feeds, score_headline  # noqa: E402


async def push(items: list[dict], engine: str, token: str) -> tuple[int, int]:
    """Send a batch. Returns (accepted, duplicates)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{engine.rstrip('/')}/api/sentiment/ingest",
                                 json={"items": items},
                                 headers={"X-Ingest-Token": token})
    if resp.status_code != 200:
        raise RuntimeError(f"engine returned {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    return body.get("accepted", 0), body.get("duplicates", 0)


async def one_pass(engine: str, token: str, dry_run: bool, limit: int) -> int:
    batch = await fetch_feeds()
    if not batch.headlines:
        print("  no headlines — every feed failed or returned nothing")
        return 1

    fresh = batch.headlines[:limit]
    print(f"  {len(batch.headlines)} headlines from {batch.feeds_read} feeds "
          f"({batch.feeds_failed} failed); scoring {len(fresh)}")

    scored = [await score_headline(h) for h in fresh]
    worth = [h for h in scored if h.event_type != "noise"]

    print(f"\n  {'source':16}{'event':17}{'score':>7}{'conf':>7}  headline")
    for h in sorted(worth, key=lambda x: -abs(x.score))[:12]:
        flag = " <-- high impact" if h.event_type in HIGH_IMPACT else ""
        print(f"  {h.source[:15]:16}{h.event_type:17}{h.score:>+7.2f}{h.confidence:>7.2f}"
              f"  {h.headline[:44]}{flag}")
    print(f"\n  {len(worth)} of {len(scored)} scored as something other than noise")

    if dry_run:
        print("  --dry-run: nothing sent\n")
        return 0
    if not token:
        print("\n  SENTIMENT_INGEST_TOKEN is not set — nothing sent.")
        print("  It must match the value the engine has.\n")
        return 2

    accepted, duplicates = await push([h.as_payload() for h in scored], engine, token)
    print(f"  sent {len(scored)}: {accepted} new, {duplicates} already known\n")
    return 0


async def main(args) -> int:
    engine = os.getenv("ENGINE_URL", "http://127.0.0.1:8080")
    token = os.getenv("SENTIMENT_INGEST_TOKEN", "")
    print(f"\n  engine {engine}")

    if args.once:
        return await one_pass(engine, token, args.dry_run, args.limit)

    while True:
        try:
            await one_pass(engine, token, args.dry_run, args.limit)
        except Exception as exc:                      # a loop must not die
            print(f"  pass failed: {str(exc)[:200]}")
        await asyncio.sleep(args.every * 60)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="One pass, then stop")
    p.add_argument("--dry-run", action="store_true", help="Fetch and score, send nothing")
    p.add_argument("--every", type=int, default=15, help="Minutes between passes")
    p.add_argument("--limit", type=int, default=40, help="Headlines scored per pass")
    raise SystemExit(asyncio.run(main(p.parse_args())))
