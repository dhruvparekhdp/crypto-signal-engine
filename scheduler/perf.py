"""
Where the time goes: request timings, job timings, and event-loop stalls.

Pages were taking 8-15 s. The HTML is small (13-33 KB gzipped), so the time
is spent on the server, and in an asyncio app the usual cause is something
blocking the event loop: every scheduled job runs on the same loop that
serves the pages, so one job doing seconds of CPU work freezes every request
behind it. Rather than guess which, this measures it:

  * a middleware times every request per route (count, p50, p95, max)
  * every scheduled job is wrapped to record its duration
  * a monitor wakes every 250 ms and records any stall over 300 ms, with
    the jobs that were running at that moment — the culprit

All in memory, small and bounded. GET /api/debug/perf (admin) reports it,
and the settings page shows the top offenders.
"""
from __future__ import annotations

import asyncio
import functools
import time
from collections import defaultdict, deque

from aiohttp import web

_routes: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
_jobs: dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
_running: dict[str, float] = {}
_stalls: deque = deque(maxlen=100)
_recent_runs: deque = deque(maxlen=500)     # (name, start, end) of finished job runs
_db_ping: deque = deque(maxlen=50)


def _pct(values, q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


@web.middleware
async def timing_middleware(request: web.Request, handler):
    t0 = time.perf_counter()
    try:
        return await handler(request)
    finally:
        info = request.match_info.route.resource
        key = info.canonical if info is not None else request.path
        _routes[f"{request.method} {key}"].append((time.perf_counter() - t0) * 1000)


def wrap_job(name: str, func):
    """Record each run's duration, and mark the job as running while it runs."""
    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def run(*a, **kw):
            t0 = time.perf_counter()
            _running[name] = t0
            try:
                return await func(*a, **kw)
            finally:
                _finish(name, t0)
        return run

    @functools.wraps(func)
    def run_sync(*a, **kw):
        t0 = time.perf_counter()
        _running[name] = t0
        try:
            return func(*a, **kw)
        finally:
            _finish(name, t0)
    return run_sync


def _finish(name: str, t0: float) -> None:
    end = time.perf_counter()
    _running.pop(name, None)
    _jobs[name].append((end - t0) * 1000)
    _recent_runs.append((name, t0, end))


def _overlapping(start: float, end: float) -> list[str]:
    """Jobs that were running at any point in [start, end]: the loop monitor only
    wakes AFTER a blocking job returns, so 'running now' would always be empty."""
    names = {n for n, s, e in _recent_runs if s <= end and e >= start}
    names |= {n for n, s in _running.items() if s <= end}
    return sorted(names)


def instrument_scheduler(scheduler) -> None:
    """Wrap every job added from now on (call before setup_jobs)."""
    original = scheduler.add_job

    def add_job(func, *a, **kw):
        name = kw.get("id") or getattr(func, "__name__", "job")
        return original(wrap_job(str(name), func), *a, **kw)
    scheduler.add_job = add_job


async def loop_monitor(interval: float = 0.25, threshold: float = 0.3) -> None:
    """Record every time the loop wakes late by more than `threshold` seconds."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        now = time.perf_counter()
        late = now - t0 - interval
        if late > threshold:
            _stalls.append({"at": time.time(), "stall_ms": round(late * 1000),
                            "running": _overlapping(t0 + interval, now)})


async def db_ping_monitor(every: float = 60.0) -> None:
    """Round-trip time of `SELECT 1` to the database, once a minute."""
    from sqlalchemy import text

    from storage.database import AsyncSessionFactory
    while True:
        try:
            t0 = time.perf_counter()
            async with AsyncSessionFactory() as s:
                await s.execute(text("SELECT 1"))
            _db_ping.append((time.perf_counter() - t0) * 1000)
        except Exception:
            _db_ping.append(-1.0)
        await asyncio.sleep(every)


def report() -> dict:
    def summary(samples):
        v = list(samples)
        return {"n": len(v), "p50_ms": round(_pct(v, 0.5)), "p95_ms": round(_pct(v, 0.95)),
                "max_ms": round(max(v)) if v else 0}
    routes = sorted(((k, summary(v)) for k, v in _routes.items()),
                    key=lambda kv: -kv[1]["p95_ms"])
    jobs = sorted(((k, summary(v)) for k, v in _jobs.items()),
                  key=lambda kv: -kv[1]["max_ms"])
    blame: dict[str, int] = defaultdict(int)
    for s in _stalls:
        for j in s["running"] or ["(no job: request or other code)"]:
            blame[j] += s["stall_ms"]
    pings = [p for p in _db_ping if p >= 0]
    return {
        "routes": [dict(route=k, **v) for k, v in routes[:25]],
        "jobs": [dict(job=k, **v) for k, v in jobs[:25]],
        "stalls": list(_stalls)[-20:],
        "stall_blame_ms": dict(sorted(blame.items(), key=lambda kv: -kv[1])),
        "db_ping_ms": {"p50": round(_pct(pings, 0.5)), "max": round(max(pings)) if pings else 0,
                       "failures": sum(1 for p in _db_ping if p < 0), "n": len(_db_ping)},
    }
