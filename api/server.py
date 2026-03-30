"""FastAPI application — outer API server for managing CUA runs.

Runs outside Modal sandboxes (as a Modal web endpoint or standalone).
Creates sandboxes on demand and proxies status/streaming from them.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import modal
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from modal import FilePatternMatcher
from opentelemetry import trace as otel_trace  # StatusCode used below
from starlette.responses import Response

from api.auth import auth_settings, verify_api_key
from api.models import RunConfig, RunResponse, RunStatus
from api.run_registry import InMemoryRunRegistry, RunHandle
from exceptions import ConfigError
from recording.manager import scan_recording_artifacts
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

# ---------------------------------------------------------------------------
# Modal app
# ---------------------------------------------------------------------------

_project_root = Path(__file__).resolve().parent.parent

_exclude_dirs = FilePatternMatcher(
    "output/**", "tests/**", "llm/**", ".git/**", "playbooks/definitions/**"
)
_include_exts = ~FilePatternMatcher(
    "**/*.py",
    "**/*.js",
    "**/*.json",
    "**/*.yaml",
    "**/*.yml",
    "**/*.toml",
    "**/*.lock",
    "**/*.sh",
)
_ignore = lambda path: _exclude_dirs(path) or _include_exts(path)  # noqa: E731

modal_app = modal.App(
    name="cua",
    image=modal.Image.debian_slim(python_version="3.13")
    .add_local_dir(
        str(_project_root),
        remote_path="/opt/cua",
        copy=True,
        ignore=_ignore,
    )
    .env({"PYTHONPATH": "/opt/cua"})
    .uv_sync(str(_project_root), extra_options="--no-dev"),
    secrets=[modal.Secret.from_name("llm-secret")],
)

# --- API key authentication ---
_security = HTTPBearer(auto_error=False)


async def _verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),  # noqa: B008
) -> None:
    await verify_api_key(credentials)


_run_registry = InMemoryRunRegistry()

_http_client: httpx.AsyncClient | None = None


def _remove_run_registry(run_id: str) -> None:
    _run_registry.remove(run_id)


def _get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise RuntimeError("HTTP client not initialized: _http_client is None")
    return _http_client


async def _cleanup_finished_sandbox(run_id: str) -> bool:
    handle = _run_registry.get(run_id)
    if handle is None:
        return False

    try:
        exit_code = await handle.sandbox.poll.aio()
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
    environment, api_key = auth_settings()
    if not api_key:
        if environment == "local":
            log.warning(
                "CUA_API_KEY not set — API endpoints are unauthenticated (local mode)"
            )
        else:
            log.error(
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


@modal_app.function(timeout=3600)
@modal.asgi_app()
def serve():
    """Serve the CUA FastAPI app as a Modal web endpoint."""
    return web_app


@web_app.get("/public-key")
async def public_key() -> Response:
    """Return the server's RSA public key for credential encryption."""
    from credentials import get_public_key_pem

    settings = get_settings()
    if not settings.cua_private_key_pem.get_secret_value():
        raise HTTPException(
            status_code=503,
            detail="Credential encryption is not configured (CUA_PRIVATE_KEY_PEM not set)",
        )
    pem = get_public_key_pem(settings.cua_private_key_pem.get_secret_value().encode())
    return Response(content=pem, media_type="application/x-pem-file")


@web_app.post("/runs", response_model=RunResponse)
async def create_run(config: RunConfig) -> RunResponse:
    """Create a new CUA run by spawning a Modal sandbox."""
    from sandbox.image import PORT_STATUS, create_cua_sandbox

    # Decrypt encrypted credentials if provided
    decrypted_credentials = None
    if config.encrypted_credentials:
        from credentials import decrypt_credentials

        settings = get_settings()
        private_key_pem = settings.cua_private_key_pem.get_secret_value()
        if not private_key_pem:
            raise HTTPException(
                status_code=400,
                detail="encrypted_credentials sent but CUA_PRIVATE_KEY_PEM not configured",
            )
        try:
            decrypted_credentials = decrypt_credentials(
                config.encrypted_credentials,
                private_key_pem.encode(),
            )
        except (ValueError, ConfigError) as exc:
            log.warning("Credential decryption failed: %s", exc)
            raise HTTPException(
                status_code=400,
                detail="Invalid or corrupted encrypted credentials",
            ) from exc

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
                sandbox = await create_cua_sandbox(
                    config,
                    modal_app,
                    credentials=decrypted_credentials,
                    extra_env=trace_ctx,
                )
                run_id = sandbox.object_id

            session_span.set_attribute(ATTR_SESSION_ID, run_id)

            tunnels = await sandbox.tunnels.aio()
            status_base = tunnels[PORT_STATUS].url
        except Exception as exc:
            log.exception("Failed to create sandbox for run %s", run_id or "unknown")
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            session_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            session_span.record_exception(exc)
            # Log full error server-side; return sanitized message to client
            error_msg = str(exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Failed to create sandbox",
                    "run_id": run_id,
                    "message": error_msg
                    if get_settings().environment == "local"
                    else "Internal error — check server logs",
                },
            ) from exc

        assert sandbox is not None
        _run_registry.add(
            RunHandle(run_id=run_id, sandbox=sandbox, status_base_url=status_base)
        )

        log.info("Created run %s", run_id)

        return RunResponse(
            run_id=run_id,
            status_url=f"/runs/{run_id}",
            stream_url=f"/runs/{run_id}/stream",
        )


@web_app.get("/runs/{run_id}", response_model=RunStatus)
async def get_run_status(run_id: str) -> RunStatus:
    """Get the status of a CUA run."""
    if await _cleanup_finished_sandbox(run_id):
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


@web_app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    """Terminate a CUA run early."""
    handle = _run_registry.get(run_id)
    if not handle:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    await handle.sandbox.terminate.aio()
    _remove_run_registry(run_id)
    log.info("Terminated run %s", run_id)
    return {"status": "terminated", "run_id": run_id}


@web_app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """Proxy SSE events from the sandbox's internal status API."""
    if await _cleanup_finished_sandbox(run_id):
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


# ---------------------------------------------------------------------------
# Recording retrieval — reads from Modal Volume or proxies from live sandbox
# ---------------------------------------------------------------------------

_VOLUME_MOUNT = "/recordings"


def _volume_path(run_id: str, *parts: str) -> Path:
    """Build a path inside the recordings volume, guarded against traversal."""
    base = Path(_VOLUME_MOUNT) / run_id
    result = base.joinpath(*parts).resolve()
    if not str(result).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    return result


@web_app.get("/runs/{run_id}/recording/manifest")
async def get_recording_manifest(run_id: str) -> dict:
    """List recording artifacts. Proxies to sandbox if live, reads volume if completed."""
    handle = _run_registry.get(run_id)
    if handle:
        client = _get_http_client()
        try:
            resp = await client.get(f"{handle.status_base_url}/recording/manifest")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError:
            pass

    # Fall back to volume
    run_dir = Path(_VOLUME_MOUNT) / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="No recordings found")

    return {"run_id": run_id, "artifacts": scan_recording_artifacts(run_dir)}


@web_app.get("/runs/{run_id}/recording/trace")
async def get_recording_trace(run_id: str) -> Response:
    """Download the Playwright trace ZIP."""
    handle = _run_registry.get(run_id)
    if handle:
        client = _get_http_client()
        try:
            # Stream from sandbox to avoid loading full ZIP into memory
            async def _proxy_trace():
                async with client.stream(
                    "GET",
                    f"{handle.status_base_url}/recording/trace",
                    timeout=None,
                ) as upstream:
                    upstream.raise_for_status()
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

            return StreamingResponse(
                _proxy_trace(),
                media_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="trace.zip"'},
            )
        except httpx.HTTPError:
            pass

    path = _volume_path(run_id, "trace.zip")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Trace not available")
    return FileResponse(path, media_type="application/zip", filename="trace.zip")


@web_app.get("/runs/{run_id}/recording/screenshots/{filename}")
async def get_recording_screenshot(run_id: str, filename: str) -> FileResponse:
    """Download an individual screenshot."""
    safe_name = Path(filename).name
    path = _volume_path(run_id, "screenshots", safe_name)
    if not path.exists() or path.suffix != ".jpg":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(path, media_type="image/jpeg")
