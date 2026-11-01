"""
Liveness endpoint — crash hardening (2026-11-01).

The Docker HEALTHCHECK used to run `python -c "import main"` every 60s: a
full second Python importing the whole app chain (~200 MB + real CPU work),
competing with the RUNNING bot it was supposed to watch, and probing
nothing about it. Three slow checks — exactly what happens during a signal
burst or the spaCy-loading startup window — made Docker kill the container:
the hard-kill, no-traceback crash signature of 2026-10-30/31.

This module replaces that probe with the real thing: a minimal HTTP server
ON the bot's own event loop, bound to 127.0.0.1 (never exposed), serving
/health. Semantics:

  - Responds from the very top of startup (main.py starts it first), so
    the whole initialization window is covered — no more startup kills.
  - Runs on the shared event loop: a hung or blocked loop cannot answer →
    healthcheck fails → Railway restarts. TRUE liveness, not a file check.
  - Near-zero cost: one tiny GET per interval vs a full app import.

The payload carries status / uptime / paper_trading / environment for the
deployment logs. This is LIVENESS only — never readiness or health scoring
(the health SCORE lives in monitoring/health_score.py and gates entries).
"""

import asyncio
import logging
import time
from typing import Optional

from aiohttp import web

import config

logger = logging.getLogger(__name__)

_started_at = time.monotonic()


def create_health_app() -> web.Application:
    """Build the liveness app (factory kept pure for tests)."""
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - _started_at, 1),
            "paper_trading": bool(config.PAPER_TRADING),
            "environment": config.ENVIRONMENT,
        })

    app.router.add_get("/health", health)
    return app


async def start_health_server(port: Optional[int] = None) -> web.AppRunner:
    """Bind the liveness endpoint and return the runner (tests read
    runner.addresses for the actual port). The caller owns the runner's
    lifetime; run_health_server cleans it up on cancellation."""
    runner = web.AppRunner(create_health_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port or config.HEALTH_CHECK_PORT)
    await site.start()
    logger.info(
        "[HEALTH_SERVER] Liveness endpoint serving on 127.0.0.1:%s/health",
        site._port,
    )
    return runner


async def run_health_server() -> None:
    """Serve /health forever on the shared event loop (supervisor pattern:
    CancelledError re-raises after cleanup)."""
    runner = await start_health_server()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise
    finally:
        await runner.cleanup()
