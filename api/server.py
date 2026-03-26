"""FastAPI application — outer API server for managing CUA runs.

Runs outside Modal sandboxes (as a Modal web endpoint or standalone).
Creates sandboxes on demand and proxies status/streaming from them.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import httpx
import modal
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import trace as otel_trace  # StatusCode used below

from api.models import RunConfig, RunResponse, RunStatus
from api.run_registry import InMemoryRunRegistry, RunHandle
from sandbox.image import PORT_NOVNC, PORT_STATUS, create_cua_sandbox
from settings import get_settings
from telemetry import get_tracer, setup_telemetry
from telemetry.metrics import active_sessions, sessions_total
from telemetry.middleware import instrument_fastapi
from telemetry.propagation import inject_trace_context
from telemetry.spans import (
    ATTR_DIRECTIVE,
    ATTR_DISPLAY_HEIGHT,
    ATTR_DISPLAY_WIDTH,
    ATTR_MAX_STEPS,
    ATTR_MODEL,
    ATTR_PROFILE,
    ATTR_SESSION_ID,
    ATTR_START_URL,
    SANDBOX_CREATE,
    SESSION,
)

log = logging.getLogger(__name__)

modal_app = modal.App("cua")

# --- API key authentication ---
_API_KEY = get_settings().cua_api_key or None
_security = HTTPBearer(auto_error=False)


async def _verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),  # noqa: B008
) -> None:
    """Verify Bearer token if CUA_API_KEY is set. Skip auth if unset (local dev)."""
    if not _API_KEY:
        return  # No auth configured — local dev mode
    if credentials is None or credentials.credentials != _API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


_run_registry = InMemoryRunRegistry()

_http_client: httpx.AsyncClient | None = None


def _remove_run_registry(run_id: str) -> None:
    _run_registry.remove(run_id)


def _get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized: _http_client is None")
    return _http_client


def _cleanup_finished_sandbox(run_id: str) -> bool:
    handle = _run_registry.get(run_id)
    if handle is None:
        return False

    try:
        exit_code = handle.sandbox.poll()
    except Exception:
        log.exception("Failed to poll sandbox for run %s", run_id)
        return False

    if exit_code is None:
        return False

    log.info(
        "Cleaning up finished sandbox for run %s (exit code %s)", run_id, exit_code
    )
    _remove_run_registry(run_id)
    return True


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    global _http_client
    setup_telemetry("cua-api")
    instrument_fastapi(app_instance)
    _http_client = httpx.AsyncClient(timeout=10)
    if not _API_KEY:
        log.warning("CUA_API_KEY not set — API endpoints are unauthenticated")
    yield
    await _http_client.aclose()
    _http_client = None


app = FastAPI(
    title="Computer Use Agent API",
    lifespan=lifespan,
    dependencies=[Depends(_verify_api_key)],
)


@app.post("/runs", response_model=RunResponse)
async def create_run(config: RunConfig) -> RunResponse:
    """Create a new CUA run by spawning a Modal sandbox."""
    tracer = get_tracer()

    with tracer.start_as_current_span(
        SESSION,
        attributes={
            ATTR_DIRECTIVE: config.directive[:500],
            ATTR_MODEL: config.model,
            ATTR_MAX_STEPS: config.max_steps,
            ATTR_PROFILE: config.profile,
            ATTR_DISPLAY_WIDTH: config.display_width,
            ATTR_DISPLAY_HEIGHT: config.display_height,
            ATTR_START_URL: config.start_url or "",
        },
    ) as session_span:
        active_sessions().add(1)
        sandbox: modal.Sandbox | None = None
        run_id: str | None = None
        try:
            with tracer.start_as_current_span(SANDBOX_CREATE):
                # Inject trace context into sandbox env vars
                trace_ctx = inject_trace_context()
                sandbox = create_cua_sandbox(config, modal_app, extra_env=trace_ctx)
                run_id = sandbox.object_id

            session_span.set_attribute(ATTR_SESSION_ID, run_id)

            tunnels = sandbox.tunnels()
            novnc_url = tunnels[PORT_NOVNC].url
            status_base = tunnels[PORT_STATUS].url
        except Exception as exc:
            log.exception("Failed to create sandbox for run %s", run_id or "unknown")
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            session_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            session_span.record_exception(exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Failed to create sandbox",
                    "run_id": run_id,
                    "message": str(exc),
                },
            ) from exc

        assert sandbox is not None
        _run_registry.add(
            RunHandle(run_id=run_id, sandbox=sandbox, status_base_url=status_base)
        )

        log.info("Created run %s, noVNC: %s", run_id, novnc_url)

        return RunResponse(
            run_id=run_id,
            novnc_url=novnc_url,
            status_url=f"/runs/{run_id}",
            stream_url=f"/runs/{run_id}/stream",
        )


@app.get("/runs/{run_id}", response_model=RunStatus)
async def get_run_status(run_id: str) -> RunStatus:
    """Get the status of a CUA run."""
    if _cleanup_finished_sandbox(run_id):
        return RunStatus(
            run_id=run_id,
            status="terminated",
            error="Sandbox has already exited",
        )

    handle = _run_registry.get(run_id)
    if not handle:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    client = _get_http_client()
    try:
        resp = await client.get(f"{handle.status_base_url}/status")
        resp.raise_for_status()
        data = resp.json()
        return RunStatus(**data)
    except httpx.HTTPError as exc:
        _remove_run_registry(run_id)
        log.warning("Status request failed for run %s: %s", run_id, exc)
        return RunStatus(
            run_id=run_id,
            status="terminated",
            error="Sandbox is no longer reachable",
        )


@app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    """Terminate a CUA run early."""
    handle = _run_registry.get(run_id)
    if not handle:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    handle.sandbox.terminate()
    _remove_run_registry(run_id)
    log.info("Terminated run %s", run_id)
    return {"status": "terminated", "run_id": run_id}


@app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Proxy SSE events from the sandbox's internal status API."""
    if _cleanup_finished_sandbox(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    handle = _run_registry.get(run_id)
    if not handle:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    client = _get_http_client()

    async def proxy_events():
        try:
            async with client.stream(
                "GET", f"{handle.status_base_url}/events", timeout=None
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    yield line + "\n"
        except httpx.HTTPError as e:
            _remove_run_registry(run_id)
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        proxy_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
