"""In-sandbox status server (port 8090).

Runs inside each Modal sandbox alongside the agent loop. Exposes:
- GET /status — current RunStatus
- GET /events — SSE stream of ActionLog events (supports multiple consumers)

The agent loop pushes events via push_action() / complete_run().
Started by the entrypoint script before the agent loop begins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from actionlog.actions import SSE_EXCLUDED_FIELDS, ActionLog, format_sse_event

log = logging.getLogger(__name__)

app = FastAPI(title="CUA Sandbox Status")

# Shared state — set by the agent loop, read by the status endpoints
_status: dict = {
    "run_id": "",
    "status": "starting",
    "action_count": 0,
    "actions": [],
    "result": None,
    "error": None,
    "duration_ms": None,
}
_subscribers: list[asyncio.Queue[ActionLog | None]] = []
_run_start: float = 0.0
_COMPLETION_SENTINEL_TIMEOUT_SECONDS = 1.0


def init_status(run_id: str) -> None:
    """Initialize status state. Called once at sandbox startup."""
    global _run_start
    _status["run_id"] = run_id
    _status["status"] = "running"
    _run_start = time.monotonic()


def push_action(action: ActionLog) -> None:
    """Push an action event from the agent loop. Non-blocking."""
    _status["action_count"] = action.step
    _status["actions"].append(
        {k: v for k, v in asdict(action).items() if k not in SSE_EXCLUDED_FIELDS}
    )
    _status["duration_ms"] = int((time.monotonic() - _run_start) * 1000)
    for q in list(_subscribers):
        try:
            q.put_nowait(action)
        except asyncio.QueueFull:
            log.warning("Subscriber queue full, dropping action %d", action.step)


async def complete_run(summary: str | None = None, error: str | None = None) -> None:
    """Mark the run as completed or failed. Called by the agent loop on exit."""
    _status["status"] = "failed" if error else "completed"
    _status["result"] = summary
    _status["error"] = error
    _status["duration_ms"] = int((time.monotonic() - _run_start) * 1000)
    for q in list(_subscribers):
        try:
            await asyncio.wait_for(
                q.put(None),
                timeout=_COMPLETION_SENTINEL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            log.error("Timed out sending completion sentinel to subscriber")
        except Exception:
            log.exception("Cannot send completion sentinel to subscriber")


@app.get("/status")
async def get_status() -> dict:
    """Return current run status."""
    return _status


@app.get("/events")
async def get_events() -> StreamingResponse:
    """SSE stream of action events. Each consumer gets its own queue."""

    async def event_generator():
        q: asyncio.Queue[ActionLog | None] = asyncio.Queue(maxsize=200)
        _subscribers.append(q)
        try:
            while True:
                action = await q.get()
                if action is None:
                    yield f"event: complete\ndata: {json.dumps({'status': _status['status']})}\n\n"
                    return
                yield format_sse_event(action)
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
