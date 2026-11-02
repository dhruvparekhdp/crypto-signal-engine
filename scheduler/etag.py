"""ETag / If-None-Match for GET /api/* (UI rebuild plan §2 0b)."""
from __future__ import annotations

import hashlib

from aiohttp import web


def _etag_for_body(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


@web.middleware
async def etag_middleware(request: web.Request, handler):
    if request.method != "GET" or not request.path.startswith("/api/"):
        return await handler(request)

    resp = await handler(request)
    if not isinstance(resp, web.Response) or isinstance(resp, web.FileResponse):
        return resp
    if resp.status != 200:
        return resp
    body = resp.body
    if body is None and resp.text is not None:
        body = resp.text.encode(resp.charset or "utf-8")
    if body is None:
        return resp

    tag = _etag_for_body(body)
    resp.headers["ETag"] = tag
    client = request.headers.get("If-None-Match", "").strip()
    if client and client == tag:
        return web.Response(status=304, headers={"ETag": tag})
    return resp
