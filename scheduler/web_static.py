"""Serve the Angular build at /app/ (UI rebuild plan §6.1)."""
from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

_DIST = Path(__file__).resolve().parents[1] / "web" / "dist" / "browser"
_HASHED = re.compile(r"\.[a-f0-9]{8,}\.(js|css|mjs|map)$", re.I)


def dist_root() -> Path:
    return _DIST


async def _serve_app(request: web.Request) -> web.Response:
    rel = request.match_info.get("path", "") or ""
    rel = rel.lstrip("/")
    if rel and ".." in rel.split("/"):
        raise web.HTTPForbidden()
    target = (_DIST / rel).resolve()
    if not str(target).startswith(str(_DIST.resolve())):
        raise web.HTTPForbidden()
    if target.is_file():
        resp = web.FileResponse(target)
        if _HASHED.search(target.name):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
    index = _DIST / "index.html"
    if not index.is_file():
        return web.Response(
            text="Angular app not built. Run npm run build in web/.",
            status=503,
            content_type="text/plain",
        )
    return web.FileResponse(index)


def register_web_app(app: web.Application) -> None:
    app.router.add_get("/app", _serve_app)
    app.router.add_get("/app/", _serve_app)
    app.router.add_get("/app/{path:.*}", _serve_app)
