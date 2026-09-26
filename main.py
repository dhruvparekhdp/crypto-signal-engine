"""
Entry point for the crypto signal engine.

Starts the aiohttp web server (dashboard + JSON API) and the APScheduler
job loop that polls live market data, runs the signal detectors, and drives
the paper-trading simulator.

The web server binds before init_db on purpose: the database is remote, and
a slow first connection used to hold the port closed long enough for health
checks to fail against a process that was otherwise fine.

Usage:
    python main.py

Environment:
    Copy .env.example to .env and fill in your credentials. Everything that
    is not a secret is configured at runtime from /settings, stored in the DB.
"""
import asyncio
import os
import signal

import structlog

from config.logging_config import configure_logging
from scheduler.health import start_health_server
from scheduler.runner import AppRunner
from storage.database import init_db
import storage.models  # noqa: F401 — registers all tables on Base.metadata before create_all

log = structlog.get_logger()


async def main() -> None:
    configure_logging()
    log.info("crypto_signal_engine_starting")
    import sys

    from scheduler.alerts import excepthook
    sys.excepthook = excepthook     # record an error that kills the process

    # Render/EC2 injects PORT; fall back to 8080 locally
    port = int(os.environ.get("PORT", 8080))

    runner = AppRunner()
    health_runner = await start_health_server(runner, port=port)
    log.info("web_server_started", port=port, health_url=f"http://0.0.0.0:{port}/health")

    try:
        await init_db()
        log.info("database_initialised")
    except Exception:
        log.exception("database_initialisation_failed")

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown_signal_received")
        stop_event.set()

    import sys
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except (NotImplementedError, AttributeError):
                pass

    await runner.start()
    log.info("monitor_running", health_url=f"http://localhost:{port}/health")

    await stop_event.wait()

    log.info("shutting_down")
    await runner.stop()
    from scheduler.alerts import mark_clean_stop
    mark_clean_stop()           # a stop we asked for, not a crash
    await health_runner.cleanup()
    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
