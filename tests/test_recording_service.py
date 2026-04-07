"""Tests for live recording trace proxy behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api.recording_service import RecordingService
from api.runs.registry import RunHandle


class _FakeClient:
    def __init__(self, response) -> None:
        self._response = response

    def build_request(self, method: str, url: str):
        return SimpleNamespace(method=method, url=url)

    async def send(self, request, *, stream: bool = False):
        assert request.method == "GET"
        assert stream is True
        return self._response


class _FakeUpstreamResponse:
    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self._chunks = chunks
        self.aclose = AsyncMock()

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def _make_service(tmp_path, response) -> RecordingService:
    volume = SimpleNamespace(reload=SimpleNamespace(aio=AsyncMock()))
    handle = RunHandle(
        run_id="run-123",
        sandbox=None,
        status_base_url="http://sandbox.test",
    )
    return RecordingService(
        volume_mount=str(tmp_path),
        volume=volume,
        get_http_client=lambda: _FakeClient(response),
        get_handle=AsyncMock(return_value=handle),
    )


async def _collect_bytes(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_get_trace_falls_back_to_persisted_file_before_any_live_bytes(tmp_path):
    run_dir = tmp_path / "run-123"
    run_dir.mkdir()
    (run_dir / "trace.zip").write_bytes(b"persisted-trace")

    response = _FakeUpstreamResponse([httpx.ReadError("stream failed")])
    service = _make_service(tmp_path, response)

    trace_response = await service.get_trace("run-123")
    payload = await _collect_bytes(trace_response)

    assert payload == b"persisted-trace"
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_trace_does_not_append_persisted_file_after_partial_live_stream(
    tmp_path,
):
    run_dir = tmp_path / "run-123"
    run_dir.mkdir()
    (run_dir / "trace.zip").write_bytes(b"persisted-trace")

    response = _FakeUpstreamResponse(
        [b"live-prefix", httpx.ReadError("stream failed after first chunk")]
    )
    service = _make_service(tmp_path, response)

    trace_response = await service.get_trace("run-123")
    payload = await _collect_bytes(trace_response)

    assert payload == b"live-prefix"
    response.aclose.assert_awaited_once()
