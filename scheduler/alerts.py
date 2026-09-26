"""
Tell the owner the moment something breaks, so it can be fixed in real time.

Two kinds of alert, both on Telegram:

  crash    on start, if the previous run did not stop cleanly: what the last
           error was and where, or "killed without an error" (usually the
           kernel's out-of-memory killer) with the command to confirm it
  error    while running, any error logged by the engine (a failed job, a
           feed that throws), with the exception and the file:line it came
           from; the same error alerts at most once per 30 minutes

The error side is a structlog processor, so every existing log.exception /
log.error call already reports without being touched. Alerts are queued and
sent by a background task, so logging never waits on Telegram.
"""
from __future__ import annotations

import asyncio
import html
import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

COOLDOWN_S = 30 * 60
_queue: asyncio.Queue | None = None
_last_sent: dict[str, float] = {}
_state_dir = Path("data")


def configure(state_dir: Path) -> None:
    global _state_dir
    _state_dir = Path(state_dir)


def _where(tb) -> str:
    """The deepest frame inside this project: 'analysis/foo.py:123 in bar'."""
    root = str(Path(__file__).resolve().parent.parent)
    best = ""
    for fr in traceback.extract_tb(tb):
        if fr.filename.startswith(root) and "site-packages" not in fr.filename:
            best = f"{fr.filename[len(root) + 1:]}:{fr.lineno} in {fr.name}"
    return best


def _summary(event_dict: dict) -> tuple[str, str]:
    """(error text, where) for an error log event."""
    exc = event_dict.get("exc_info")
    if exc is True:
        exc = sys.exc_info()
    if isinstance(exc, BaseException):
        exc = (type(exc), exc, exc.__traceback__)
    if isinstance(exc, tuple) and exc[0] is not None:
        return f"{exc[0].__name__}: {str(exc[1])[:300]}", _where(exc[2])
    detail = event_dict.get("error") or event_dict.get("reason") or ""
    return str(detail)[:300], ""


def processor(logger, method_name, event_dict):
    """structlog processor: queue a Telegram alert for error-level events."""
    try:
        if method_name not in ("error", "exception", "critical") \
                and not event_dict.get("exc_info"):
            return event_dict
        event = str(event_dict.get("event", "error"))
        text, where = _summary(event_dict)
        _remember(event, text, where)
        now = time.time()
        if now - _last_sent.get(event, 0) < COOLDOWN_S:
            return event_dict
        _last_sent[event] = now
        if _queue is not None:
            _queue.put_nowait(
                f"⚠️ <b>Error</b>: <code>{html.escape(event)}</code>\n"
                + (f"{html.escape(text)}\n" if text else "")
                + (f"at <code>{html.escape(where)}</code>\n" if where else "")
                + "<i>Same error stays quiet for 30 min.</i>")
    except Exception:
        pass
    return event_dict


def _remember(event: str, text: str, where: str) -> None:
    """Keep the last error on disk, for the crash report after a restart."""
    try:
        _state_dir.mkdir(parents=True, exist_ok=True)
        (_state_dir / "last_error.json").write_text(json.dumps({
            "at": datetime.now(UTC).isoformat(), "event": event, "error": text,
            "where": where}))
    except OSError:
        pass


def excepthook(exc_type, exc, tb) -> None:
    """An exception that kills the process: record it before dying."""
    _remember("uncaught_exception", f"{exc_type.__name__}: {str(exc)[:300]}", _where(tb))
    sys.__excepthook__(exc_type, exc, tb)


def mark_running(sha: str) -> dict | None:
    """
    Record that this run has started, and return the previous run's state if it
    did NOT stop cleanly (a crash), else None.
    """
    path = _state_dir / "run_state.json"
    previous = None
    try:
        previous = json.loads(path.read_text()) if path.exists() else None
    except (OSError, ValueError):
        previous = None
    try:
        _state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sha": sha, "started": datetime.now(UTC).isoformat(),
                                    "clean": False}))
    except OSError:
        pass
    if previous and not previous.get("clean"):
        return previous
    return None


def mark_clean_stop() -> None:
    path = _state_dir / "run_state.json"
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
        state["clean"] = True
        path.write_text(json.dumps(state))
    except (OSError, ValueError):
        pass


def crash_report(previous: dict) -> str:
    """The Telegram text for a crash detected on start."""
    last = {}
    try:
        p = _state_dir / "last_error.json"
        last = json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        last = {}
    started = previous.get("started", "")
    fresh = last and last.get("at", "") >= started
    msg = ("💥 <b>Engine crashed and restarted</b>\n"
           f"Run started {html.escape(started[:16].replace('T', ' '))} UTC "
           f"on <code>{html.escape(str(previous.get('sha', '?')))}</code>.\n")
    if fresh:
        msg += (f"Last error: <code>{html.escape(last.get('event', ''))}</code>\n"
                f"{html.escape(last.get('error', ''))}\n"
                + (f"at <code>{html.escape(last.get('where', ''))}</code>\n"
                   if last.get("where") else ""))
    else:
        msg += ("It was killed without logging an error, usually the kernel's "
                "out-of-memory killer. Check: <code>sudo dmesg | grep -i oom</code>\n")
    return msg + "Logs: <code>sudo journalctl -u crypto-engine -n 200</code>"


async def sender(notify) -> None:
    """Drain the alert queue into Telegram. Never raises."""
    global _queue
    _queue = asyncio.Queue(maxsize=100)
    while True:
        text = await _queue.get()
        try:
            await notify(text)
        except Exception:
            pass
        await asyncio.sleep(1)      # stay inside Telegram's rate limits
