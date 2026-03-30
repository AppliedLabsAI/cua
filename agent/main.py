"""CLI entry point for the CUA agent — runs inside the Modal sandbox.

Reads configuration from environment variables (set by the entrypoint.sh),
initializes the bridge and browser, runs the agent loop, and reports results
to the in-sandbox status API.

The status API (uvicorn) is started in-process as an asyncio task so it
shares module globals (``_status``, ``_subscribers``, ``_action_log``) with
the agent loop — this is how ``push_action`` and ``complete_run`` propagate
events to SSE subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

from telemetry.logging import setup_logging

setup_logging()
log = logging.getLogger("cua.agent")

STATUS_API_PORT = 8090


async def _start_status_api() -> asyncio.Task:
    """Start the in-sandbox status API as a background asyncio task.

    Runs uvicorn in the same process so the status API shares module
    globals with the agent loop (push_action / complete_run update the
    same _status and _subscribers objects that GET /events reads from).
    """
    import uvicorn

    from api.streaming import app

    uvi_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=STATUS_API_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(uvi_config)
    task = asyncio.create_task(server.serve())
    # Give uvicorn a moment to bind the port
    await asyncio.sleep(0.5)
    return task


async def main() -> int:
    # Import here to avoid import errors when checking syntax outside sandbox
    from agent.session_runner import run_sandbox_session
    from api.streaming import init_status
    from config import CUAConfig
    from telemetry import setup_telemetry
    from telemetry.propagation import extract_trace_context

    # Initialize OTel for the inner agent process
    setup_telemetry("cua-agent")

    # Extract trace context propagated from outer API via env vars
    parent_ctx = extract_trace_context(
        os.environ.get("TRACEPARENT", ""),
        os.environ.get("TRACESTATE", ""),
    )

    # Load all configuration from environment
    try:
        config = CUAConfig.from_env()
    except (ValueError, Exception) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    from settings import get_settings

    # Use sandbox object ID as run ID (set by Modal)
    run_id = get_settings().modal_sandbox_id

    log.info(
        "Starting CUA agent: model=%s, max_steps=%d, %dx%d, profile=%s",
        config.model,
        config.max_steps,
        config.width,
        config.height,
        config.profile_name,
    )
    log.info("Directive: %s", config.directive[:200])

    # Start status API in-process (shares globals with agent loop)
    status_task = await _start_status_api()
    log.info("Status API started on port %d (in-process)", STATUS_API_PORT)

    # Initialize status API state
    init_status(run_id)
    result = await run_sandbox_session(
        run_id=run_id,
        config=config,
        parent_ctx=parent_ctx,
    )

    # Cancel the status API after a grace period for final SSE delivery
    await asyncio.sleep(1)
    status_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await status_task

    return result


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
