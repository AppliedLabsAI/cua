"""Tests for persisted run helpers and RunService fallbacks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.models import RunStatus, RunStatusValue
from api.runs.registry import InMemoryRunRegistry
from api.runs.service import RunService
from api.runs.store import PersistedRunStore


def _persisted_status(run_id: str) -> RunStatus:
    return RunStatus(
        run_id=run_id,
        status=RunStatusValue.COMPLETED,
        action_count=1,
        actions=[
            {
                "step": 1,
                "timestamp": "2026-03-31T00:00:00Z",
                "tool": "browser_dom",
                "action": "click",
                "input_summary": "click '#save'",
                "duration_ms": 10,
                "success": True,
            }
        ],
        result="done",
    )


async def _collect_stream(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return "".join(chunks)


@pytest.mark.asyncio
async def test_persisted_run_store_loads_status(tmp_path):
    run_id = "run-123"
    status = _persisted_status(run_id)
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "status.json").write_text(status.model_dump_json())

    volume = SimpleNamespace(reload=SimpleNamespace(aio=AsyncMock()))
    store = PersistedRunStore(volume_mount=str(tmp_path), volume=volume)

    loaded = await store.load_status(run_id)

    assert loaded == status
    volume.reload.aio.assert_awaited_once()


@pytest.mark.asyncio
async def test_persisted_run_store_builds_replay_stream(tmp_path):
    run_id = "run-123"
    status = _persisted_status(run_id)
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "status.json").write_text(status.model_dump_json())

    volume = SimpleNamespace(reload=SimpleNamespace(aio=AsyncMock()))
    store = PersistedRunStore(volume_mount=str(tmp_path), volume=volume)

    response = await store.build_event_stream(run_id, start_after=0)

    assert response is not None
    payload = await _collect_stream(response)
    normalized = payload.replace(" ", "")
    assert 'id:1\ndata:{"step":1' in normalized
    assert f'"status":"{RunStatusValue.COMPLETED}"' in payload.replace(" ", "")


def _make_service(tmp_path) -> RunService:
    volume = SimpleNamespace(reload=SimpleNamespace(aio=AsyncMock()))
    return RunService(
        registry=InMemoryRunRegistry(),
        modal_app=MagicMock(),
        volume_mount=str(tmp_path),
        volume=volume,
        get_http_client=MagicMock,
    )


@pytest.mark.asyncio
async def test_get_status_returns_persisted_status_when_handle_missing(
    tmp_path, monkeypatch
):
    run_id = "run-123"
    status = _persisted_status(run_id)
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "status.json").write_text(status.model_dump_json())

    service = _make_service(tmp_path)
    monkeypatch.setattr(
        service, "cleanup_finished_sandbox", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(service, "get_handle", AsyncMock(return_value=None))

    result = await service.get_status(run_id)

    assert result == status


@pytest.mark.asyncio
async def test_stream_run_uses_persisted_replay_when_handle_missing(
    tmp_path, monkeypatch
):
    run_id = "run-123"
    status = _persisted_status(run_id)
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "status.json").write_text(status.model_dump_json())

    service = _make_service(tmp_path)
    monkeypatch.setattr(
        service, "cleanup_finished_sandbox", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(service, "get_handle", AsyncMock(return_value=None))

    response = await service.stream_run(run_id, MagicMock(headers={}))

    payload = await _collect_stream(response)
    normalized = payload.replace(" ", "")
    assert 'id:1\ndata:{"step":1' in normalized
    assert f'"status":"{RunStatusValue.COMPLETED}"' in payload.replace(" ", "")


@pytest.mark.asyncio
async def test_stream_run_raises_not_found_without_handle_or_persisted_state(
    tmp_path, monkeypatch
):
    service = _make_service(tmp_path)
    monkeypatch.setattr(
        service, "cleanup_finished_sandbox", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(service, "get_handle", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc:
        await service.stream_run("missing", MagicMock(headers={}))

    detail = json.dumps(exc.value.detail)
    assert "NOT_FOUND" in detail
