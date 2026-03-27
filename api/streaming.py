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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from actionlog.actions import ActionLog, format_sse_event
from api.models import RunStatus
from recording import DEFAULT_OUTPUT_DIR
from recording.manager import scan_recording_artifacts

log = logging.getLogger(__name__)

app = FastAPI(title="CUA Sandbox Status")

# Shared state — set by the agent loop, read by the status endpoints.
_status = RunStatus(run_id="", status="starting")
_subscribers: list[asyncio.Queue[ActionLog | None]] = []
_run_start: float = 0.0
_COMPLETION_SENTINEL_TIMEOUT_SECONDS = 1.0


def init_status(run_id: str) -> None:
    """Initialize status state. Called once at sandbox startup."""
    global _run_start
    _status.run_id = run_id
    _status.status = "running"
    _status.action_count = 0
    _status.actions.clear()
    _status.result = None
    _status.error = None
    _status.duration_ms = None
    _run_start = time.monotonic()


def push_action(action: ActionLog) -> None:
    """Push an action event from the agent loop. Non-blocking."""
    _status.action_count = action.step
    _status.actions.append(action.to_event())
    _status.duration_ms = int((time.monotonic() - _run_start) * 1000)
    for q in list(_subscribers):
        try:
            q.put_nowait(action)
        except asyncio.QueueFull:
            log.warning("Subscriber queue full, dropping action %d", action.step)


async def complete_run(
    summary: str | None = None,
    error: str | None = None,
    data: dict[str, Any] | None = None,
    extracted_texts: list[str] | None = None,
) -> None:
    """Mark the run as completed or failed. Called by the agent loop on exit."""
    _status.status = "failed" if error else "completed"
    _status.result = summary
    _status.error = error
    _status.data = data
    _status.extracted_texts = extracted_texts or []
    _status.duration_ms = int((time.monotonic() - _run_start) * 1000)
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
async def get_status() -> RunStatus:
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
                    payload = {"status": _status.status}
                    yield f"event: complete\ndata: {json.dumps(payload)}\n\n"
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


# ---------------------------------------------------------------------------
# Recording retrieval endpoints
# ---------------------------------------------------------------------------

_RECORDING_DIR = Path(DEFAULT_OUTPUT_DIR)


@app.get("/recording/manifest")
async def get_recording_manifest() -> dict:
    """List available recording artifacts."""
    return {
        "run_id": _status.run_id,
        "artifacts": scan_recording_artifacts(_RECORDING_DIR),
    }


@app.get("/recording/trace")
async def get_recording_trace() -> FileResponse:
    """Download the Playwright trace ZIP."""
    trace_path = _RECORDING_DIR / "trace.zip"
    if not trace_path.exists():
        raise HTTPException(status_code=404, detail="Trace not available")
    return FileResponse(trace_path, media_type="application/zip", filename="trace.zip")


@app.get("/recording/screenshots/{filename}")
async def get_recording_screenshot(filename: str) -> FileResponse:
    """Download an individual screenshot."""
    safe_name = Path(filename).name
    path = _RECORDING_DIR / "screenshots" / safe_name
    if not path.exists() or path.suffix != ".jpg":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(path, media_type="image/jpeg")
