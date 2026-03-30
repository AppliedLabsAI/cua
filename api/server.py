"""FastAPI application wiring for the outer CUA API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import modal
from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.auth import auth_settings, verify_api_key
from api.modal_app import VOLUME_MOUNT, modal_app, recording_volume
from api.models import RunConfig, RunResponse, RunStatus
from api.recording_service import RecordingService
from api.run_registry import InMemoryRunRegistry
from api.run_service import RunService
from telemetry import setup_telemetry
from telemetry.middleware import instrument_fastapi

logger = logging.getLogger(__name__)

# --- API key authentication ---
_security = HTTPBearer(auto_error=False)


async def _verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),  # noqa: B008
) -> None:
    await verify_api_key(credentials)


_run_registry = InMemoryRunRegistry()

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized: _http_client is None")
    return _http_client


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    global _http_client
    setup_telemetry("cua-api")
    instrument_fastapi(app_instance)
    _http_client = httpx.AsyncClient(timeout=10)
    environment, api_key = auth_settings()
    if not api_key:
        if environment == "local":
            logger.warning(
                "CUA_API_KEY not set — API endpoints are unauthenticated (local mode)"
            )
        else:
            logger.error(
                "CUA_API_KEY not set — API requests will be rejected in production mode"
            )
    yield
    await _http_client.aclose()
    _http_client = None


web_app = FastAPI(
    title="Computer Use Agent API",
    lifespan=lifespan,
    dependencies=[Depends(_verify_api_key)],
)
_run_service = RunService(
    registry=_run_registry,
    modal_app=modal_app,
    volume_mount=VOLUME_MOUNT,
    volume=recording_volume,
    get_http_client=_get_http_client,
)
_recording_service = RecordingService(
    volume_mount=VOLUME_MOUNT,
    volume=recording_volume,
    get_http_client=_get_http_client,
    get_handle=_run_service.get_handle,
)


@modal_app.function(
    timeout=3600,
    volumes={VOLUME_MOUNT: recording_volume},
)
@modal.asgi_app()
def serve():
    """Serve the CUA FastAPI app as a Modal web endpoint."""
    return web_app


@web_app.post("/runs", response_model=RunResponse)
async def create_run(config: RunConfig) -> RunResponse:
    return await _run_service.create_run(config)


@web_app.get("/runs/{run_id}", response_model=RunStatus)
async def get_run_status(run_id: str) -> RunStatus:
    return await _run_service.get_status(run_id)


@web_app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    return await _run_service.stop_run(run_id)


@web_app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> StreamingResponse:
    return await _run_service.stream_run(run_id, request)


@web_app.get("/runs/{run_id}/recording/manifest")
async def get_recording_manifest(run_id: str) -> dict:
    return await _recording_service.get_manifest(run_id)


@web_app.get("/runs/{run_id}/recording/trace")
async def get_recording_trace(run_id: str):
    return await _recording_service.get_trace(run_id)
