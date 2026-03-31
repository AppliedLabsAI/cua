"""Tests for SSE replay, event IDs, and run registry lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest

from actionlog.actions import ActionLog, format_sse_event
from api import streaming
from api.errors import ApiErrorCode
from api.models import RunStatus
from api.runs.registry import InMemoryRunRegistry, RunHandle, RunPhase

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(step: int, action: str = "click") -> ActionLog:
    return ActionLog(
        step=step,
        timestamp="2026-01-01T00:00:00Z",
        tool="browser_dom",
        action=action,
        input_summary=f"{action} step {step}",
        tool_input={"selector": f"#btn-{step}"},
        duration_ms=100,
        success=True,
    )


# ---------------------------------------------------------------------------
# format_sse_event — event ID field
# ---------------------------------------------------------------------------


class TestFormatSseEvent:
    def test_includes_event_id(self):
        action = _make_action(step=5)
        result = format_sse_event(action)
        assert result.startswith("id: 5\n")

    def test_includes_data_line(self):
        action = _make_action(step=1)
        result = format_sse_event(action)
        lines = result.strip().split("\n")
        assert lines[0] == "id: 1"
        assert lines[1].startswith("data: ")
        payload = json.loads(lines[1][len("data: ") :])
        assert payload["step"] == 1
        assert payload["action"] == "click"

    def test_ends_with_double_newline(self):
        action = _make_action(step=1)
        result = format_sse_event(action)
        assert result.endswith("\n\n")


# ---------------------------------------------------------------------------
# RunRegistry — phase tracking
# ---------------------------------------------------------------------------


class TestRunRegistryPhase:
    def test_default_phase_is_running(self):
        handle = RunHandle(run_id="r1", sandbox=None, status_base_url="http://x")
        assert handle.phase == "running"

    def test_phase_transition_to_terminated(self):
        handle = RunHandle(run_id="r1", sandbox=None, status_base_url="http://x")
        handle.phase = RunPhase.TERMINATED
        assert handle.phase == RunPhase.TERMINATED

    def test_registry_add_get_remove(self):
        reg = InMemoryRunRegistry()
        handle = RunHandle(run_id="r1", sandbox=None, status_base_url="http://x")
        reg.add(handle)
        assert reg.get("r1") is handle
        removed = reg.remove("r1")
        assert removed is handle
        assert reg.get("r1") is None

    def test_registry_remove_nonexistent(self):
        reg = InMemoryRunRegistry()
        assert reg.remove("nonexistent") is None

    def test_error_field(self):
        handle = RunHandle(
            run_id="r1",
            sandbox=None,
            status_base_url="http://x",
            phase="failed",
            error={"code": "INTERNAL_ERROR", "message": "boom"},
        )
        assert handle.error is not None
        assert handle.error.message == "boom"


# ---------------------------------------------------------------------------
# streaming.py — replay support
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_streaming_state():
    """Reset module-level state in streaming.py between tests."""
    streaming._status = RunStatus(run_id="", status="starting")
    streaming._subscribers.clear()
    streaming._action_log.clear()
    streaming._run_start = 0.0
    yield
    streaming._subscribers.clear()
    streaming._action_log.clear()


class TestPushAction:
    def test_appends_to_action_log(self):
        streaming.init_status("test-run")
        a1 = _make_action(1)
        a2 = _make_action(2)
        streaming.push_action(a1)
        streaming.push_action(a2)
        assert len(streaming._action_log) == 2
        assert streaming._action_log[0].step == 1
        assert streaming._action_log[1].step == 2

    def test_updates_status_action_count(self):
        streaming.init_status("test-run")
        streaming.push_action(_make_action(1))
        streaming.push_action(_make_action(2))
        assert streaming._status.action_count == 2

    def test_broadcasts_to_subscribers(self):
        streaming.init_status("test-run")
        q: asyncio.Queue[ActionLog | None] = asyncio.Queue(maxsize=10)
        streaming._subscribers.append(q)
        streaming.push_action(_make_action(1))
        assert not q.empty()
        item = q.get_nowait()
        assert item is not None
        assert item.step == 1


class TestInitStatus:
    def test_clears_action_log(self):
        streaming._action_log.append(_make_action(1))
        streaming.init_status("new-run")
        assert len(streaming._action_log) == 0
        assert streaming._status.run_id == "new-run"
        assert streaming._status.status == "running"


class TestCompleteRun:
    @pytest.mark.asyncio
    async def test_sets_completed_status(self):
        streaming.init_status("test-run")
        await streaming.complete_run(summary="Done")
        assert streaming._status.status == "completed"
        assert streaming._status.result == "Done"

    @pytest.mark.asyncio
    async def test_sets_failed_status_on_error(self):
        streaming.init_status("test-run")
        await streaming.complete_run(error="Something broke")
        assert streaming._status.status == "failed"
        assert streaming._status.error is not None
        assert streaming._status.error.code == ApiErrorCode.INTERNAL_ERROR
        assert streaming._status.error.message == "Something broke"

    @pytest.mark.asyncio
    async def test_sends_sentinel_to_subscribers(self):
        streaming.init_status("test-run")
        q: asyncio.Queue[ActionLog | None] = asyncio.Queue(maxsize=10)
        streaming._subscribers.append(q)
        await streaming.complete_run(summary="Done")
        item = q.get_nowait()
        assert item is None  # sentinel


# ---------------------------------------------------------------------------
# SSE replay via /events endpoint
# ---------------------------------------------------------------------------


class TestSseReplay:
    @pytest.mark.asyncio
    async def test_replays_past_events(self):
        """Subscriber connecting after events were pushed receives all past events."""
        streaming.init_status("test-run")
        streaming.push_action(_make_action(1))
        streaming.push_action(_make_action(2))
        streaming.push_action(_make_action(3))
        await streaming.complete_run(summary="Done")

        from starlette.testclient import TestClient

        client = TestClient(streaming.app)
        resp = client.get("/events", timeout=5)
        assert resp.status_code == 200

        lines = resp.text.strip().split("\n")
        # Should have id + data lines for 3 actions + complete event
        id_lines = [line for line in lines if line.startswith("id: ")]
        assert len(id_lines) == 3
        assert id_lines[0] == "id: 1"
        assert id_lines[1] == "id: 2"
        assert id_lines[2] == "id: 3"

        # Last event should be the complete event
        assert "event: complete" in resp.text

    @pytest.mark.asyncio
    async def test_last_event_id_skips_replayed(self):
        """Last-Event-ID header causes replay to skip earlier events."""
        streaming.init_status("test-run")
        streaming.push_action(_make_action(1))
        streaming.push_action(_make_action(2))
        streaming.push_action(_make_action(3))
        await streaming.complete_run(summary="Done")

        from starlette.testclient import TestClient

        client = TestClient(streaming.app)
        resp = client.get("/events", headers={"Last-Event-ID": "2"}, timeout=5)
        assert resp.status_code == 200

        lines = resp.text.strip().split("\n")
        id_lines = [line for line in lines if line.startswith("id: ")]
        # Only step 3 should be replayed (steps 1-2 skipped)
        assert len(id_lines) == 1
        assert id_lines[0] == "id: 3"

    @pytest.mark.asyncio
    async def test_replay_completed_run_sends_complete_event(self):
        """Connecting to a completed run replays events then sends complete."""
        streaming.init_status("test-run")
        streaming.push_action(_make_action(1))
        await streaming.complete_run(summary="All done")

        from starlette.testclient import TestClient

        client = TestClient(streaming.app)
        resp = client.get("/events", timeout=5)
        assert "event: complete" in resp.text
        assert '"completed"' in resp.text

    @pytest.mark.asyncio
    async def test_status_endpoint(self):
        """GET /status returns current run status."""
        streaming.init_status("test-run")
        streaming.push_action(_make_action(1))

        from starlette.testclient import TestClient

        client = TestClient(streaming.app)
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "test-run"
        assert data["status"] == "running"
        assert data["action_count"] == 1


# ---------------------------------------------------------------------------
# RunResponse model — status field
# ---------------------------------------------------------------------------


class TestRunResponseModel:
    def test_includes_status_field(self):
        from api.models import RunResponse

        resp = RunResponse(
            run_id="r1",
            status="running",
            status_url="/runs/r1",
            stream_url="/runs/r1/stream",
        )
        assert resp.status == "running"

    def test_default_status_is_starting(self):
        from api.models import RunResponse

        resp = RunResponse(
            run_id="r1",
            status_url="/runs/r1",
            stream_url="/runs/r1/stream",
        )
        assert resp.status == "starting"
