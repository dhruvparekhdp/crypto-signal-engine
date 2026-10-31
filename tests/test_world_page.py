"""/world: the 3D World news page with three looks (owner 31 Oct 2026: "a,b and c all design with switch, and deploy")."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from scheduler.news_portal_api import country_heat, event_countries


def test_official_press_without_a_country_belongs_to_its_bank():
    assert event_countries("[]", "central_bank_rss:fed") == ["US"]
    assert event_countries("[]", "central_bank_rss:ecb") == ["EU"]
    assert event_countries('["cn"]', "regional_rss") == ["CN"]
    assert event_countries("not json", "regional_rss") == []


def test_heat_scales_open_event_severity_to_the_hottest_country():
    ev = [{"status": "open", "level": 4, "countries": ["US"]}, {"status": "open", "level": 2, "countries": ["US", "CN"]},
          {"status": "resolved", "level": 5, "countries": ["CN"]}]
    assert country_heat(ev) == {"US": 1.0, "CN": round(2 / 6, 3)}
    assert country_heat([]) == {}


@pytest.mark.asyncio
async def test_the_world_api_returns_events_with_source_and_heat(monkeypatch):
    from config.settings import settings
    from scheduler import news_portal_api as api
    from storage.database import Base
    from storage.news_models import NewsDocument, NewsEvent, NewsEventStory, NewsOddsSnapshot
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    S = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with S() as s:
        s.add(NewsDocument(id=1, source_id="central_bank_rss:fed", external_id="a", title="FOMC statement",
                           url="https://www.federalreserve.gov/x", published_at=now))
        s.add(NewsEvent(id=1, event_key="k1", title="FOMC statement", event_type="central_bank", severity=4,
                        countries_json="[]", status="open", started_at=now - timedelta(hours=1)))
        s.add(NewsEventStory(event_id=1, document_id=1))
        s.add(NewsOddsSnapshot(venue="polymarket", market_id="m", title="Fed cut in December?", yes_prob=0.62,
                               volume_usd=5e6, captured_at=now))
        await s.commit()
    monkeypatch.setattr(api, "AsyncSessionFactory", S)
    monkeypatch.setattr(settings, "news_engine_enabled", True)
    monkeypatch.setattr(settings, "news_portal_widgets_enabled", True)
    import json
    body = json.loads((await api.api_portal_world(None)).text)
    e = body["events"][0]
    assert (e["source"], e["countries"], e["level"], e["url"]) == ("Federal Reserve press", ["US"], 4,
                                                                   "https://www.federalreserve.gov/x")
    assert body["heat"] == {"US": 1.0} and body["open"] == 1
    assert body["ticker"][0]["v"].endswith("62%")
    await eng.dispose()


@pytest.mark.asyncio
async def test_the_page_has_the_three_looks_and_loads_the_globe():
    from scheduler.world_page import GLOBE_JS, world_page
    html = (await world_page(None)).text
    for look in ("w-look-a", "w-look-b", "w-look-c"):
        assert f'id="{look}"' in html
    assert GLOBE_JS in html and "/api/portal/world" in html and "__GLOBE_JS__" not in html


def test_the_world_tab_opens_the_new_page():
    from pathlib import Path
    nav = (Path(__file__).resolve().parents[1] / "scheduler" / "portal" / "nav.js").read_text()
    assert '{ id: "world", label: "World", url: "/world" }' in nav
