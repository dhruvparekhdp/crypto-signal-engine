"""18 Oct 2026: a logged-out settings save answered 503 'not configured / set API_AUTH_TOKEN'; it must say 401 'log in'.
And the USD->INR rate is not a setting: sizing uses Delta's own live rate."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.mark.asyncio
async def test_logged_out_admin_call_says_log_in(monkeypatch):
    from scheduler import health
    from scheduler.settings_page import _admin
    monkeypatch.setattr(health._SETTINGS, "api_auth_token", None, raising=False)

    async def h(request):
        denied = await _admin(request)
        return denied if denied is not None else web.json_response({"ok": True})
    app = web.Application()
    app.router.add_post("/x", h)
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/x", json={})
        body = await r.json()
    assert r.status == 401 and "log in" in body["error"]


def test_usd_inr_is_not_an_editable_setting():
    from config.overrides import BY_KEY
    assert "delta_india_usd_inr" not in BY_KEY
