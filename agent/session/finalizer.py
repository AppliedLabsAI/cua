"""Helpers for persisting and finalizing sandbox session outcomes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.result import AgentResult

import modal
from opentelemetry import trace as otel_trace

from api.errors import ApiError, ApiErrorCode, classify_runtime_error, make_api_error
from api.models import RunStatusValue
from api.streaming import complete_run, persist_status
from telemetry.metrics import active_sessions, session_duration, sessions_total
from telemetry.spans import EVENT_AGENT_COMPLETED

logger = logging.getLogger(__name__)

_RECORDING_VOLUME_NAME = "cua-recordings"


async def _commit_recording_volume() -> None:
    """Commit the recordings volume so the outer API can read persisted data."""
    try:
        vol = modal.Volume.from_name(_RECORDING_VOLUME_NAME)
        await vol.commit.aio()
        logger.info("Committed recordings volume")
    except Exception:
        logger.warning("Failed to commit recordings volume", exc_info=True)


@dataclass(frozen=True)
class RunOutcome:
    """Structured terminal outcome for a sandbox session."""

    status: RunStatusValue
    metrics_status: str
    exit_code: int
    summary: str | None = None
    error: ApiError | str | None = None
    data: dict[str, Any] | None = None
    extracted_texts: list[str] = field(default_factory=list)
    session_memory: str = ""
    trace_status: otel_trace.StatusCode = otel_trace.StatusCode.OK
    trace_message: str | None = None

    @classmethod
    def setup_failed(cls, run_id: str, exc: Exception) -> RunOutcome:
        return cls(
            status=RunStatusValue.FAILED,
            metrics_status="failed",
            exit_code=1,
            error=make_api_error(
                ApiErrorCode.SETUP_FAILED,
                f"Setup failed: {exc}",
                details={"run_id": run_id},
            ),
            trace_status=otel_trace.StatusCode.ERROR,
            trace_message=str(exc),
        )

    @classmethod
    def terminated(
        cls,
        run_id: str,
        message: str,
        *,
        extracted_texts: list[str] | None = None,
    ) -> RunOutcome:
        return cls(
            status=RunStatusValue.TERMINATED,
            metrics_status="terminated",
            exit_code=1,
            error=make_api_error(
                ApiErrorCode.RUN_TERMINATED,
                message,
                details={"run_id": run_id},
            ),
            extracted_texts=list(extracted_texts or []),
            trace_status=otel_trace.StatusCode.ERROR,
            trace_message=message,
        )

    @classmethod
    def crashed(
        cls,
        message: str | None,
        *,
        extracted_texts: list[str] | None = None,
    ) -> RunOutcome:
        return cls(
            status=RunStatusValue.FAILED,
            metrics_status="failed",
            exit_code=1,
            error=classify_runtime_error(message),
            extracted_texts=list(extracted_texts or []),
            trace_status=otel_trace.StatusCode.ERROR,
            trace_message=message or "Unknown error",
        )

    @classmethod
    def from_agent_result(
        cls,
        result: AgentResult,
    ) -> RunOutcome:
        if result.success:
            return cls(
                status=RunStatusValue.COMPLETED,
                metrics_status="success",
                exit_code=0,
                summary=result.summary,
                data=result.data,
                extracted_texts=list(result.extracted_texts),
                session_memory=result.session_memory,
                trace_status=otel_trace.StatusCode.OK,
            )

        runtime_error = classify_runtime_error(result.error)
        if runtime_error is not None:
            error: ApiError | str | None = runtime_error
            trace_message = runtime_error.message
        else:
            trace_message = str(result.error) if result.error else "Unknown error"
            error = trace_message

        return cls(
            status=RunStatusValue.FAILED,
            metrics_status="failed",
            exit_code=1,
            error=error,
            extracted_texts=list(result.extracted_texts),
            session_memory=result.session_memory,
            trace_status=otel_trace.StatusCode.ERROR,
            trace_message=trace_message,
        )


class RunFinalizer:
    """Persist terminal session state, release resources, and record metrics."""

    def __init__(
        self,
        *,
        run_id: str,
        browser,
        recording,
        recording_upload: bool,
    ) -> None:
        self._run_id = run_id
        self._browser = browser
        self._recording = recording
        self._recording_upload = recording_upload

    async def persist(self, outcome: RunOutcome) -> None:
        """Persist terminal run state for outer-API retrieval."""
        try:
            await complete_run(
                summary=outcome.summary,
                error=outcome.error,
                data=outcome.data,
                extracted_texts=outcome.extracted_texts,
                status=outcome.status,
                session_memory=outcome.session_memory,
            )
            await persist_status(f"/recordings/{self._run_id}")
            await _commit_recording_volume()
        except Exception:
            logger.warning("Failed to persist run state", exc_info=True)

    async def cleanup(self) -> None:
        """Finalize recording artifacts and close the browser."""
        if self._recording:
            try:
                await self._recording.stop()
                if self._recording_upload:
                    await self._recording.upload(f"/recordings/{self._run_id}")
                else:
                    logger.info(
                        "Recordings available at %s",
                        self._recording.output_dir,
                    )
            except Exception as exc:
                logger.warning(
                    "Recording finalization failed: %s",
                    exc,
                    exc_info=True,
                )

        try:
            await self._browser.close()
            logger.info("Browser closed")
        except Exception:
            logger.warning("Browser close failed during cleanup", exc_info=True)

    async def finalize(
        self,
        outcome: RunOutcome,
        *,
        result=None,
    ) -> int:
        """Persist state, cleanup resources, and emit terminal metrics."""
        await self.persist(outcome)
        await self.cleanup()

        if result is not None:
            otel_trace.get_current_span().add_event(
                EVENT_AGENT_COMPLETED,
                attributes={
                    "success": result.success,
                    "summary": (result.summary or "")[:200],
                    "action_count": result.action_count,
                    "total_input_tokens": result.total_input_tokens,
                    "total_output_tokens": result.total_output_tokens,
                    "total_duration_ms": result.total_duration_ms,
                },
            )
            logger.info(
                "Stats: %d actions, %dms, %d input tokens, %d output tokens",
                result.action_count,
                result.total_duration_ms,
                result.total_input_tokens,
                result.total_output_tokens,
            )
            session_duration().record(
                result.total_duration_ms,
                {"status": outcome.metrics_status},
            )

        active_sessions().add(-1)
        sessions_total().add(1, {"status": outcome.metrics_status})
        return outcome.exit_code
