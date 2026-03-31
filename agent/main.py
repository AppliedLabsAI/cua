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
import signal
import sys

from telemetry.logging import setup_logging

setup_logging()
logger = logging.getLogger("cua.agent")

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
        logger.error("Configuration error: %s", exc)
        return 1

    from settings import get_settings

    # Use sandbox object ID as run ID (set by Modal)
    run_id = get_settings().modal_sandbox_id

    logger.info(
        "Starting CUA agent: model=%s, max_steps=%d, %dx%d, profile=%s",
        config.model,
        config.max_steps,
        config.width,
        config.height,
        config.profile_name,
    )
    logger.info("Directive: %s", config.directive[:200])

    # Start status API in-process (shares globals with agent loop)
    status_task = await _start_status_api()
    logger.info("Status API started on port %d (in-process)", STATUS_API_PORT)

    # Initialize status API state
    init_status(run_id)
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig: int) -> None:
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        try:
            sig_name = signal.Signals(sig).name
        except ValueError:
            sig_name = str(sig)
        logger.warning("Received %s, starting graceful shutdown", sig_name)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig)

    session_task = asyncio.create_task(
        run_sandbox_session(
            run_id=run_id,
            config=config,
            parent_ctx=parent_ctx,
            shutdown_event=shutdown_event,
        )
    )
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        {session_task, shutdown_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if shutdown_task in done and shutdown_event.is_set():
        session_task.cancel()
    for task in pending:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await shutdown_task
    try:
        result = await asyncio.wait_for(session_task, timeout=30)
    except TimeoutError:
        logger.error("Session task did not finish within 30s shutdown grace period")
        session_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session_task
        result = 1
    except asyncio.CancelledError:
        result = 1

    # Cancel the status API after a grace period for final SSE delivery
    await asyncio.sleep(1)
    status_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await status_task

    return result


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
