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

from api.models import RunConfig, RunResponse, RunStatus
from sandbox.image import PORT_NOVNC, PORT_STATUS, create_cua_sandbox
from settings import get_settings

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


# In-memory store of active sandbox references
_sandboxes: dict[str, modal.Sandbox] = {}
_sandbox_status_urls: dict[str, str] = {}

_http_client: httpx.AsyncClient | None = None


def _remove_run_registry(run_id: str) -> None:
    _sandboxes.pop(run_id, None)
    _sandbox_status_urls.pop(run_id, None)


def _get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized: _http_client is None")
    return _http_client


def _cleanup_finished_sandbox(run_id: str) -> bool:
    sandbox = _sandboxes.get(run_id)
    if sandbox is None:
        return False

    try:
        exit_code = sandbox.poll()
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
    sandbox: modal.Sandbox | None = None
    run_id: str | None = None
    try:
        sandbox = create_cua_sandbox(config, modal_app)
        run_id = sandbox.object_id
        tunnels = sandbox.tunnels()
        novnc_url = tunnels[PORT_NOVNC].url
        status_base = tunnels[PORT_STATUS].url
    except Exception as exc:
        log.exception("Failed to create sandbox for run %s", run_id or "unknown")
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Failed to create sandbox",
                "run_id": run_id,
                "message": str(exc),
            },
        ) from exc

    assert sandbox is not None
    _sandboxes[run_id] = sandbox
    _sandbox_status_urls[run_id] = status_base

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

    status_base = _sandbox_status_urls.get(run_id)
    if not status_base:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    client = _get_http_client()
    try:
        resp = await client.get(f"{status_base}/status")
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
    sandbox = _sandboxes.get(run_id)
    if not sandbox:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    sandbox.terminate()
    _remove_run_registry(run_id)
    log.info("Terminated run %s", run_id)
    return {"status": "terminated", "run_id": run_id}


@app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Proxy SSE events from the sandbox's internal status API."""
    if _cleanup_finished_sandbox(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    status_base = _sandbox_status_urls.get(run_id)
    if not status_base:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    client = _get_http_client()

    async def proxy_events():
        try:
            async with client.stream(
                "GET", f"{status_base}/events", timeout=None
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
