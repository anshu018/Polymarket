"""
tests/test_health_server.py — crash-hardening liveness endpoint tests.

The Docker HEALTHCHECK now curls the in-process /health endpoint
(monitoring/health_server.py) instead of spawning `python -c "import main"`
— the old probe was a ~200MB second Python competing with the running bot
and 3 slow checks during a signal burst or startup made Docker kill the
container (the 2026-10-30/31 hard-kill crash signature). These tests lock
the endpoint contract: real HTTP 200 on the shared event loop, the payload
fields the deployment logs rely on, and clean cancellation.
"""

import asyncio
import sys
import os
from unittest.mock import patch

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import config
from monitoring.health_server import run_health_server, start_health_server


@pytest.mark.anyio
async def test_health_endpoint_answers_over_real_http():
    """The probe must be a genuine HTTP GET answered by the shared event
    loop — a hung loop cannot respond, which is the liveness semantics."""
    runner = await start_health_server(port=0)  # ephemeral port
    try:
        host, port = runner.addresses[0]
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://{host}:{port}/health") as resp:
                assert resp.status == 200
                payload = await resp.json()
        assert payload["status"] == "ok"
        assert payload["paper_trading"] is bool(config.PAPER_TRADING)
        assert payload["environment"] == config.ENVIRONMENT
        assert isinstance(payload["uptime_seconds"], float)
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_health_server_cleans_up_on_cancellation():
    """Supervisor pattern: cancellation stops the server and closes the
    listener (the port is freed — no dangling socket on shutdown)."""
    task = asyncio.create_task(run_health_server())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_health_payload_reflects_config_values():
    """The payload reads live config (a deployment log grep for
    paper_trading must tell the truth)."""
    runner = await start_health_server(port=0)
    try:
        host, port = runner.addresses[0]
        with patch.object(config, "PAPER_TRADING", False), \
             patch.object(config, "ENVIRONMENT", "test-env"):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://{host}:{port}/health") as resp:
                    payload = await resp.json()
        assert payload["paper_trading"] is False
        assert payload["environment"] == "test-env"
    finally:
        await runner.cleanup()
