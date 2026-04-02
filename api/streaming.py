"""In-sandbox status server (port 8090).

Runs inside each Modal sandbox alongside the agent loop. Exposes:
- GET /status — current RunStatus
- GET /events — SSE stream of ActionLog events with replay support

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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from actionlog.actions import ActionLog, format_sse_event
from api.errors import ApiError, ApiErrorCode, coerce_api_error, raise_api_error
from api.models import RunStatus, RunStatusValue
from recording import DEFAULT_OUTPUT_DIR
from recording.manager import scan_recording_artifacts

logger = logging.getLogger(__name__)

app = FastAPI(title="CUA Sandbox Status")

# Shared state — set by the agent loop, read by the status endpoints.
_status = RunStatus(run_id="", status=RunStatusValue.STARTING)
_subscribers: list[asyncio.Queue[ActionLog | None]] = []
_action_log: list[ActionLog] = []  # Full ActionLog history for SSE replay
_run_start: float = 0.0
_COMPLETION_SENTINEL_TIMEOUT_SECONDS = 1.0


def init_status(run_id: str) -> None:
    """Initialize status state. Called once at sandbox startup."""
    global _run_start
    _status.run_id = run_id
    _status.status = RunStatusValue.RUNNING
    _status.action_count = 0
    _status.actions.clear()
    _status.result = None
    _status.error = None
    _status.duration_ms = None
    _status.data = None
    _status.extracted_texts = []
    _status.session_memory = ""
    _action_log.clear()
    _run_start = time.monotonic()


def push_action(action: ActionLog) -> None:
    """Push an action event from the agent loop. Non-blocking."""
    _status.action_count = action.step
    _status.actions.append(action.to_event())
    _status.duration_ms = int((time.monotonic() - _run_start) * 1000)
    _action_log.append(action)
    for q in list(_subscribers):
        try:
            q.put_nowait(action)
        except asyncio.QueueFull:
            logger.warning("Subscriber queue full, dropping action %d", action.step)


async def complete_run(
    summary: str | None = None,
    error: str | ApiError | dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    extracted_texts: list[str] | None = None,
    status: RunStatusValue | None = None,
    session_memory: str = "",
) -> None:
    """Mark the run as completed or failed. Called by the agent loop on exit."""
    structured_error = coerce_api_error(error, default_code=ApiErrorCode.INTERNAL_ERROR)
    _status.status = status or (
        RunStatusValue.FAILED if structured_error else RunStatusValue.COMPLETED
    )
    _status.result = summary
    _status.error = structured_error
    _status.data = data
    _status.extracted_texts = extracted_texts or []
    _status.session_memory = session_memory
    _status.duration_ms = int((time.monotonic() - _run_start) * 1000)
    for q in list(_subscribers):
        try:
            await asyncio.wait_for(
                q.put(None),
                timeout=_COMPLETION_SENTINEL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error("Timed out sending completion sentinel to subscriber")
        except Exception:
            logger.exception("Cannot send completion sentinel to subscriber")


async def persist_status(output_dir: str) -> None:
    """Save the current RunStatus to disk for post-termination retrieval.

    Called by the agent before exit so the outer API can serve status
    from the Modal Volume after the sandbox is gone.
    """
    import asyncio
    import os

    path = os.path.join(output_dir, "status.json")

    def _write() -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(_status.model_dump_json(indent=2))

    await asyncio.to_thread(_write)
    logger.info("Persisted run status to %s", path)


@app.get("/status")
async def get_status() -> RunStatus:
    """Return current run status."""
    return _status


_TERMINAL_STATUSES = frozenset(
    {
        RunStatusValue.COMPLETED,
        RunStatusValue.FAILED,
        RunStatusValue.TERMINATED,
        RunStatusValue.TIMEOUT,
    }
)


@app.get("/events")
async def get_events(request: Request) -> StreamingResponse:
    """SSE stream of action events with replay.

    New subscribers receive all past events first, then live events.
    Supports ``Last-Event-ID`` header for reconnection — only events
    after the given ID are sent.
    """
    last_id = request.headers.get("Last-Event-ID")
    try:
        start_after = int(last_id) if last_id else 0
    except ValueError:
        start_after = 0

    async def event_generator():
        q: asyncio.Queue[ActionLog | None] = asyncio.Queue(maxsize=200)
        # Subscribe before replay so events pushed during replay land in queue
        _subscribers.append(q)
        try:
            # Phase 1: Replay past events
            snapshot = list(_action_log)
            last_replayed = start_after
            for action in snapshot:
                if action.step > start_after:
                    yield format_sse_event(action)
                    last_replayed = action.step

            # If already completed, send final event and close
            if _status.status in _TERMINAL_STATUSES:
                yield f"event: complete\ndata: {json.dumps({'status': _status.status})}\n\n"
                return

            # Phase 2: Live stream (dedup against replayed events)
            while True:
                action = await q.get()
                if action is None:
                    yield f"event: complete\ndata: {json.dumps({'status': _status.status})}\n\n"
                    return
                if action.step > last_replayed:
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
        raise_api_error(404, ApiErrorCode.TRACE_NOT_AVAILABLE, "Trace not available")
    return FileResponse(trace_path, media_type="application/zip", filename="trace.zip")
