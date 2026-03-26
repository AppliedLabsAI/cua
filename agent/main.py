"""CLI entry point for the CUA agent — runs inside the Modal sandbox.

Reads configuration from environment variables (set by the entrypoint.sh),
initializes the bridge and browser, runs the agent loop, and reports results
to the in-sandbox status API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from telemetry.logging import setup_logging

setup_logging()
log = logging.getLogger("cua.agent")


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

    # Initialize status API
    init_status(run_id)
    return await run_sandbox_session(
        run_id=run_id,
        config=config,
        parent_ctx=parent_ctx,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
