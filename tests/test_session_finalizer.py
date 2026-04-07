"""Tests for sandbox session finalization helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace as otel_trace

from agent.session.finalizer import RunFinalizer, RunOutcome
from api.errors import ApiError, ApiErrorCode
from api.models import RunStatusValue


class _AgentResult(SimpleNamespace):
    """Minimal stub matching AgentResult fields used by RunOutcome.from_agent_result.

    Expected fields: success, summary, data, extracted_texts, error,
    action_count, total_duration_ms, total_input_tokens, total_output_tokens.
    """

    success: bool


def test_run_outcome_success_maps_agent_result():
    outcome = RunOutcome.from_agent_result(
        _AgentResult(
            success=True,
            summary="done",
            data={"ok": True},
            extracted_texts=["x"],
            session_memory="",
        )
    )

    assert outcome.status is RunStatusValue.COMPLETED
    assert outcome.metrics_status == "success"
    assert outcome.exit_code == 0
    assert outcome.summary == "done"


def test_run_outcome_failure_classifies_agent_error():
    outcome = RunOutcome.from_agent_result(
        _AgentResult(
            success=False,
            error="Guardrail blocked: out of scope",
            extracted_texts=["x"],
            session_memory="",
        )
    )

    assert outcome.status is RunStatusValue.FAILED
    assert outcome.exit_code == 1
    assert outcome.trace_status is otel_trace.StatusCode.ERROR
    assert isinstance(outcome.error, ApiError)
    assert outcome.error.code is ApiErrorCode.GUARDRAIL_BLOCKED


def test_run_outcome_terminated_preserves_session_memory():
    outcome = RunOutcome.terminated(
        "run-123",
        "Stopped",
        extracted_texts=["x"],
        session_memory="<session_progress>partial</session_progress>",
    )

    assert outcome.status is RunStatusValue.TERMINATED
    assert outcome.session_memory == "<session_progress>partial</session_progress>"


def test_run_outcome_crashed_preserves_session_memory():
    outcome = RunOutcome.crashed(
        "boom",
        extracted_texts=["x"],
        session_memory="<session_progress>partial</session_progress>",
    )

    assert outcome.status is RunStatusValue.FAILED
    assert outcome.session_memory == "<session_progress>partial</session_progress>"


@pytest.mark.asyncio
async def test_run_finalizer_persists_cleans_up_and_records_metrics():
    browser = SimpleNamespace(close=AsyncMock())
    recording = SimpleNamespace(
        stop=AsyncMock(),
        upload=AsyncMock(),
        output_dir="/tmp/recording",
    )
    result = _AgentResult(
        success=True,
        summary="done",
        action_count=3,
        total_input_tokens=11,
        total_output_tokens=7,
        total_duration_ms=321,
    )
    active_sessions = MagicMock()
    sessions_total = MagicMock()
    session_duration = MagicMock()
    span = MagicMock()
    call_order: list[str] = []

    async def _mark(name: str) -> None:
        call_order.append(name)

    async def _close_browser() -> None:
        await _mark("browser.close")

    async def _stop_recording() -> None:
        await _mark("recording.stop")

    async def _upload_recording(_path: str) -> None:
        await _mark("recording.upload")

    async def _complete_run(**_kwargs) -> None:
        await _mark("complete_run")

    async def _persist_status(_path: str) -> None:
        await _mark("persist_status")

    async def _commit_volume() -> None:
        await _mark("commit_recording_volume")

    browser.close.side_effect = _close_browser
    recording.stop.side_effect = _stop_recording
    recording.upload.side_effect = _upload_recording

    with (
        patch(
            "agent.session.finalizer.complete_run",
            new=AsyncMock(side_effect=_complete_run),
        ) as complete_run,
        patch(
            "agent.session.finalizer.persist_status",
            new=AsyncMock(side_effect=_persist_status),
        ) as persist_status,
        patch(
            "agent.session.finalizer._commit_recording_volume",
            new=AsyncMock(side_effect=_commit_volume),
        ) as commit_recording_volume,
        patch("agent.session.finalizer.active_sessions", return_value=active_sessions),
        patch("agent.session.finalizer.sessions_total", return_value=sessions_total),
        patch(
            "agent.session.finalizer.session_duration", return_value=session_duration
        ),
        patch("agent.session.finalizer.otel_trace.get_current_span", return_value=span),
    ):
        finalizer = RunFinalizer(
            run_id="run-123",
            browser=browser,
            recording=recording,
            recording_upload=True,
        )
        outcome = RunOutcome(
            status=RunStatusValue.COMPLETED,
            metrics_status="success",
            exit_code=0,
            summary="done",
            extracted_texts=["x"],
            session_memory="<session_progress>done</session_progress>",
        )

        exit_code = await finalizer.finalize(outcome, result=result)

    assert exit_code == 0
    complete_run.assert_awaited_once()
    await_args = complete_run.await_args
    assert await_args is not None
    assert (
        await_args.kwargs["session_memory"]
        == "<session_progress>done</session_progress>"
    )
    persist_status.assert_awaited_once_with("/recordings/run-123")
    commit_recording_volume.assert_awaited_once()
    recording.stop.assert_awaited_once()
    recording.upload.assert_awaited_once_with("/recordings/run-123")
    browser.close.assert_awaited_once()
    span.add_event.assert_called_once()
    active_sessions.add.assert_called_once_with(-1)
    sessions_total.add.assert_called_once_with(1, {"status": "success"})
    session_duration.record.assert_called_once_with(321, {"status": "success"})
    assert call_order == [
        "complete_run",
        "recording.stop",
        "recording.upload",
        "browser.close",
        "persist_status",
        "commit_recording_volume",
    ]
