"""Run lifecycle services for the outer API."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import modal
from fastapi import Request
from fastapi.responses import StreamingResponse
from opentelemetry import trace as otel_trace
from pydantic import ValidationError

from api.errors import (
    ApiError,
    ApiErrorCode,
    classify_runtime_error,
    raise_api_error,
)
from api.models import RunConfig, RunResponse, RunStatus, RunStatusValue
from api.run_registry import RunHandle, RunPhase, RunRegistry
from settings import get_settings
from telemetry import get_tracer
from telemetry.metrics import active_sessions, sessions_total
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

logger = logging.getLogger(__name__)


class RunService:
    """Manage sandbox-backed CUA runs for the API layer."""

    def __init__(
        self,
        *,
        registry: RunRegistry,
        modal_app: modal.App,
        volume_mount: str,
        volume: modal.Volume,
        get_http_client: Callable[[], httpx.AsyncClient],
    ) -> None:
        self._registry = registry
        self._modal_app = modal_app
        self._volume_mount = volume_mount
        self._volume = volume
        self._get_http_client = get_http_client
        self._active_run_ids: set[str] = set()

    def remove_handle(self, run_id: str) -> None:
        self._registry.remove(run_id)

    def _mark_run_active(self, run_id: str) -> None:
        self._active_run_ids.add(run_id)

    def _mark_run_inactive(self, run_id: str) -> None:
        if run_id in self._active_run_ids:
            self._active_run_ids.remove(run_id)
            active_sessions().add(-1)

    async def _terminated_status(self, run_id: str, error: str | ApiError) -> RunStatus:
        persisted = await self.load_persisted_status(run_id)
        if persisted:
            return persisted
        return RunStatus(
            run_id=run_id,
            status=RunStatusValue.TERMINATED,
            error=classify_runtime_error(error) if isinstance(error, str) else error,
        )

    async def load_persisted_status(self, run_id: str) -> RunStatus | None:
        """Try to load persisted status from the recordings volume."""
        try:
            await self._volume.reload.aio()
        except Exception:
            logger.debug("Volume reload failed", exc_info=True)
        status_path = Path(self._volume_mount) / run_id / "status.json"
        if not status_path.exists():
            return None
        try:
            return RunStatus.model_validate_json(status_path.read_text())
        except (ValidationError, ValueError, OSError) as exc:
            logger.warning(
                "Failed to read persisted status for run %s: %s",
                run_id,
                exc,
                exc_info=True,
            )
            return None

    async def get_handle(self, run_id: str) -> RunHandle | None:
        """Look up a run handle, reconstructing from Modal if needed."""
        from sandbox.image import PORT_STATUS

        handle = self._registry.get(run_id)
        if handle is not None:
            return handle

        try:
            sandbox = await modal.Sandbox.from_id.aio(run_id)
            tunnels = await sandbox.tunnels.aio()
            url = tunnels[PORT_STATUS].url
            handle = RunHandle(run_id=run_id, sandbox=sandbox, status_base_url=url)
            self._registry.add(handle)
            logger.info("Reconstructed handle for run %s from Modal", run_id)
            return handle
        except (modal.exception.NotFoundError, KeyError) as exc:
            logger.debug("Could not reconstruct handle for run %s: %s", run_id, exc)
            return None
        except Exception:
            logger.warning(
                "Could not reconstruct handle for run %s", run_id, exc_info=True
            )
            return None

    async def cleanup_finished_sandbox(self, run_id: str) -> bool:
        """Evict local registry state when a tracked sandbox has exited."""
        handle = self._registry.get(run_id)
        if handle is None:
            return False

        try:
            exit_code = await handle.sandbox.poll.aio()
        except Exception:
            logger.exception("Failed to poll sandbox for run %s", run_id)
            return False

        if exit_code is None:
            return False

        logger.info(
            "Cleaning up finished sandbox for run %s (exit code %s)", run_id, exit_code
        )
        self.remove_handle(run_id)
        self._mark_run_inactive(run_id)
        return True

    async def create_run(self, config: RunConfig) -> RunResponse:
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
                        self._modal_app,
                        credentials=config.credentials,
                        extra_env=trace_ctx,
                    )
                    run_id = sandbox.object_id

                session_span.set_attribute(ATTR_SESSION_ID, run_id)

                tunnels = await sandbox.tunnels.aio()
                status_base = tunnels[PORT_STATUS].url
            except Exception as exc:
                logger.exception(
                    "Failed to create sandbox for run %s", run_id or "unknown"
                )
                active_sessions().add(-1)
                sessions_total().add(1, {"status": "failed"})
                session_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
                session_span.record_exception(exc)
                error_msg = str(exc)
                raise_api_error(
                    502,
                    ApiErrorCode.SANDBOX_CREATE_FAILED,
                    error_msg
                    if get_settings().environment == "local"
                    else "Internal error — check server logs",
                    details={"run_id": run_id},
                )

            assert sandbox is not None
            assert run_id is not None
            self._registry.add(
                RunHandle(run_id=run_id, sandbox=sandbox, status_base_url=status_base)
            )
            self._mark_run_active(run_id)
            logger.info("Created run %s", run_id)

            return RunResponse(
                run_id=run_id,
                status=RunStatusValue.RUNNING,
                status_url=f"/runs/{run_id}",
                stream_url=f"/runs/{run_id}/stream",
            )

    async def get_status(self, run_id: str) -> RunStatus:
        """Return live or persisted status for a run."""
        if await self.cleanup_finished_sandbox(run_id):
            return await self._terminated_status(run_id, "Sandbox has already exited")

        handle = await self.get_handle(run_id)
        if not handle:
            persisted = await self.load_persisted_status(run_id)
            if persisted:
                return persisted
            raise_api_error(
                404,
                ApiErrorCode.NOT_FOUND,
                f"Run {run_id} not found",
                details={"run_id": run_id},
            )
        assert handle is not None

        if handle.phase == RunPhase.TERMINATED:
            return RunStatus(run_id=run_id, status=RunStatusValue.TERMINATED)

        client = self._get_http_client()
        try:
            resp = await client.get(f"{handle.status_base_url}/status")
            resp.raise_for_status()
            return RunStatus.model_validate(resp.json())
        except (ValidationError, ValueError, TypeError) as exc:
            self.remove_handle(run_id)
            self._mark_run_inactive(run_id)
            logger.warning(
                "Invalid status payload for run %s: %s",
                run_id,
                exc,
                exc_info=True,
            )
            return await self._terminated_status(
                run_id,
                ApiError(
                    code=ApiErrorCode.INVALID_STATUS_PAYLOAD,
                    message="Sandbox returned an invalid status payload",
                    details={"run_id": run_id},
                ),
            )
        except httpx.HTTPError as exc:
            self.remove_handle(run_id)
            self._mark_run_inactive(run_id)
            logger.warning("Status request failed for run %s: %s", run_id, exc)
            return await self._terminated_status(
                run_id,
                ApiError(
                    code=ApiErrorCode.SANDBOX_UNREACHABLE,
                    message="Sandbox is no longer reachable",
                    details={"run_id": run_id},
                ),
            )

    async def stop_run(self, run_id: str) -> dict[str, str | RunStatusValue]:
        """Terminate a CUA run early."""
        handle = await self.get_handle(run_id)
        if not handle:
            raise_api_error(
                404,
                ApiErrorCode.NOT_FOUND,
                f"Run {run_id} not found",
                details={"run_id": run_id},
            )
        assert handle is not None

        handle.phase = RunPhase.TERMINATED
        try:
            await handle.sandbox.terminate.aio()
        except modal.exception.NotFoundError:
            logger.debug("Sandbox already gone for run %s", run_id)
        except Exception as exc:
            logger.warning(
                "Terminate call failed for run %s: %s", run_id, exc, exc_info=True
            )

        self.remove_handle(run_id)
        self._mark_run_inactive(run_id)
        logger.info("Terminated run %s", run_id)
        return {"status": RunStatusValue.TERMINATED, "run_id": run_id}

    async def build_persisted_event_stream(
        self, run_id: str, start_after: int = 0
    ) -> StreamingResponse | None:
        """Build an SSE response from persisted status on the volume."""
        persisted = await self.load_persisted_status(run_id)
        if not persisted:
            return None

        async def replay():
            for action in persisted.actions:
                if action.step > start_after:
                    payload = action.model_dump()
                    yield f"id: {action.step}\ndata: {json.dumps(payload)}\n\n"
            yield (
                f"event: complete\ndata: {json.dumps({'status': persisted.status})}\n\n"
            )

        return StreamingResponse(
            replay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def stream_run(self, run_id: str, request: Request) -> StreamingResponse:
        """Proxy SSE events from the sandbox status API with persisted fallback."""
        last_event_id = request.headers.get("Last-Event-ID")
        try:
            start_after = int(last_event_id) if last_event_id else 0
        except ValueError:
            start_after = 0

        if await self.cleanup_finished_sandbox(run_id):
            resp = await self.build_persisted_event_stream(run_id, start_after)
            if resp:
                return resp
            raise_api_error(
                404,
                ApiErrorCode.NOT_FOUND,
                f"Run {run_id} not found",
                details={"run_id": run_id},
            )

        handle = await self.get_handle(run_id)
        if not handle:
            resp = await self.build_persisted_event_stream(run_id, start_after)
            if resp:
                return resp
            raise_api_error(
                404,
                ApiErrorCode.NOT_FOUND,
                f"Run {run_id} not found",
                details={"run_id": run_id},
            )
        assert handle is not None

        client = self._get_http_client()

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
            except httpx.HTTPError as exc:
                self.remove_handle(run_id)
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

        return StreamingResponse(
            proxy_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
