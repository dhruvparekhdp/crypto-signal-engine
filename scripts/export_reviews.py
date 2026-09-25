"""
Export every AI review with the news it saw, for analysis on the laptop.

    python scripts/export_reviews.py --days 30 > reviews.jsonl

One JSON object per line: the review (pre-trade, hold/close, post-trade),
the market mood at that moment (sentiment score, Fear & Greed), the news
paragraph the model was shown, and the web briefing it was part of. That is
the dataset for asking the local model questions like "which kinds of news
preceded the trades we lost" without paying for a cloud model to read it.

Run it on EC2 (it reads DATABASE_URL from .env), then copy the file over:

    scp ubuntu@crypto-engine:crypto-signal-engine/reviews.jsonl .
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from storage.database import AsyncSessionFactory, init_db  # noqa: E402
from storage.models import MarketBriefing, SignalReview  # noqa: E402


def row_to_dict(r: SignalReview, briefing: MarketBriefing | None) -> dict:
    return {
        "id": r.id,
        "at": r.created_at.isoformat() if r.created_at else None,
        "phase": r.phase,                  # pre | hold | post
        "symbol": r.symbol,
        "signal_type": r.signal_type,
        "verdict": r.verdict,
        "factors": [f for f in (r.factors or "").split(",") if f],
        "summary": r.summary,
        "confidence_delta": r.confidence_delta,
        "outcome": r.outcome,
        "pnl_pct": r.pnl_pct,
        "model": r.model,
        "sentiment_score": r.sentiment_score,
        "fear_greed": r.fear_greed,
        "news_context": r.news_context,
        "briefing": None if briefing is None else {
            "id": briefing.id,
            "at": briefing.created_at.isoformat() if briefing.created_at else None,
            "risk_tone": briefing.risk_tone,
            "summary": briefing.summary,
            "events": json.loads(briefing.events or "[]"),
        },
    }


async def main(days: int) -> int:
    await init_db()
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    async with AsyncSessionFactory() as session:
        reviews = (await session.execute(
            select(SignalReview).where(SignalReview.created_at >= since)
            .order_by(SignalReview.created_at))).scalars().all()
        ids = {r.briefing_id for r in reviews if r.briefing_id}
        briefings = {}
        if ids:
            rows = (await session.execute(
                select(MarketBriefing).where(MarketBriefing.id.in_(ids)))).scalars().all()
            briefings = {b.id: b for b in rows}
    for r in reviews:
        print(json.dumps(row_to_dict(r, briefings.get(r.briefing_id)), ensure_ascii=False))
    print(f"exported {len(reviews)} reviews from the last {days} days", file=sys.stderr)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30, help="How far back (default 30)")
    raise SystemExit(asyncio.run(main(p.parse_args().days)))
