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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from modal import FilePatternMatcher
from opentelemetry import trace as otel_trace  # StatusCode used below
from starlette.responses import Response

from api.auth import auth_settings, verify_api_key
from api.models import RunConfig, RunResponse, RunStatus, RunStatusValue
from api.run_registry import InMemoryRunRegistry, RunHandle, RunPhase
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


async def _get_or_reconstruct_handle(run_id: str) -> RunHandle | None:
    """Look up a run handle, reconstructing from Modal if not in local registry.

    This allows any API container to serve status/stream/stop requests,
    even if a different container originally created the sandbox.
    """
    from sandbox.image import PORT_STATUS

    handle = _run_registry.get(run_id)
    if handle is not None:
        return handle

    # Attempt to reconstruct from Modal's API
    try:
        sandbox = await modal.Sandbox.from_id.aio(run_id)
        tunnels = await sandbox.tunnels.aio()
        url = tunnels[PORT_STATUS].url
        handle = RunHandle(run_id=run_id, sandbox=sandbox, status_base_url=url)
        _run_registry.add(handle)
        log.info("Reconstructed handle for run %s from Modal", run_id)
        return handle
    except Exception:
        log.warning("Could not reconstruct handle for run %s", run_id, exc_info=True)
        return None


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


_VOLUME_MOUNT = "/recordings"

_recording_volume = modal.Volume.from_name(
    "cua-recordings", create_if_missing=True, version=2
)


@modal_app.function(
    timeout=3600,
    volumes={_VOLUME_MOUNT: _recording_volume},
)
@modal.asgi_app()
def serve():
    """Serve the CUA FastAPI app as a Modal web endpoint."""
    return web_app


@web_app.post("/runs", response_model=RunResponse)
async def create_run(config: RunConfig) -> RunResponse:
    """Create a new CUA run by spawning a Modal sandbox."""
    from sandbox.image import PORT_STATUS, create_cua_sandbox

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
                trace_ctx = inject_trace_context()
                sandbox = await create_cua_sandbox(
                    config,
                    modal_app,
                    credentials=config.credentials,
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
            status=RunStatusValue.RUNNING,
            status_url=f"/runs/{run_id}",
            stream_url=f"/runs/{run_id}/stream",
        )


def _load_persisted_status(run_id: str) -> RunStatus | None:
    """Try to load persisted status from the recordings volume."""
    status_path = Path(_VOLUME_MOUNT) / run_id / "status.json"
    if not status_path.exists():
        return None
    try:
        return RunStatus.model_validate_json(status_path.read_text())
    except Exception:
        log.warning("Failed to read persisted status for run %s", run_id)
        return None


@web_app.get("/runs/{run_id}", response_model=RunStatus)
async def get_run_status(run_id: str) -> RunStatus:
    """Get the status of a CUA run."""
    if await _cleanup_finished_sandbox(run_id):
        persisted = _load_persisted_status(run_id)
        if persisted:
            return persisted
        return RunStatus(
            run_id=run_id,
            status=RunStatusValue.TERMINATED,
            error="Sandbox has already exited",
        )

    handle = await _get_or_reconstruct_handle(run_id)
    if not handle:
        # Sandbox gone — try persisted status from volume
        persisted = _load_persisted_status(run_id)
        if persisted:
            return persisted
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    if handle.phase == RunPhase.TERMINATED:
        return RunStatus(run_id=run_id, status=RunStatusValue.TERMINATED)

    client = _get_http_client()
    try:
        resp = await client.get(f"{handle.status_base_url}/status")
        resp.raise_for_status()
        data = resp.json()
        return RunStatus(**data)
    except httpx.HTTPError as exc:
        _remove_run_registry(run_id)
        log.warning("Status request failed for run %s: %s", run_id, exc)
        persisted = _load_persisted_status(run_id)
        if persisted:
            return persisted
        return RunStatus(
            run_id=run_id,
            status=RunStatusValue.TERMINATED,
            error="Sandbox is no longer reachable",
        )


@web_app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str) -> dict:
    """Terminate a CUA run early."""
    handle = await _get_or_reconstruct_handle(run_id)
    if not handle:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    handle.phase = RunPhase.TERMINATED

    try:
        await handle.sandbox.terminate.aio()
    except Exception as exc:
        log.warning("Terminate call failed for run %s: %s", run_id, exc)

    _remove_run_registry(run_id)
    log.info("Terminated run %s", run_id)
    return {"status": RunStatusValue.TERMINATED, "run_id": run_id}


def _replay_persisted_events(
    run_id: str, start_after: int = 0
) -> StreamingResponse | None:
    """Build an SSE response from persisted status on the volume."""
    persisted = _load_persisted_status(run_id)
    if not persisted:
        return None

    async def replay():
        for action in persisted.actions:
            if action.step > start_after:
                payload = action.model_dump()
                yield f"id: {action.step}\ndata: {json.dumps(payload)}\n\n"
        yield f"event: complete\ndata: {json.dumps({'status': persisted.status})}\n\n"

    return StreamingResponse(
        replay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@web_app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> StreamingResponse:
    """Proxy SSE events from the sandbox's internal status API.

    Forwards ``Last-Event-ID`` so the sandbox replays missed events
    on reconnection. Falls back to persisted status from the volume
    when the sandbox is no longer running.
    """
    last_event_id = request.headers.get("Last-Event-ID")
    start_after = int(last_event_id) if last_event_id else 0

    if await _cleanup_finished_sandbox(run_id):
        resp = _replay_persisted_events(run_id, start_after)
        if resp:
            return resp
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    handle = await _get_or_reconstruct_handle(run_id)
    if not handle:
        resp = _replay_persisted_events(run_id, start_after)
        if resp:
            return resp
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    client = _get_http_client()

    async def proxy_events():
        headers: dict[str, str] = {}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            async with client.stream(
                "GET",
                f"{handle.status_base_url}/events",
                headers=headers,
                timeout=None,
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
    handle = await _get_or_reconstruct_handle(run_id)
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
    handle = await _get_or_reconstruct_handle(run_id)
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
