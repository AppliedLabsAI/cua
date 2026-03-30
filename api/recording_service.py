"""Recording artifact services for live and completed runs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.responses import Response

from api.run_registry import RunHandle
from recording.manager import scan_recording_artifacts

log = logging.getLogger(__name__)


class RecordingService:
    """Serve recording artifacts from a live sandbox or persisted volume."""

    def __init__(
        self,
        *,
        volume_mount: str,
        get_http_client: Callable[[], httpx.AsyncClient],
        get_handle: Callable[[str], Awaitable[RunHandle | None]],
    ) -> None:
        self._volume_mount = volume_mount
        self._get_http_client = get_http_client
        self._get_handle = get_handle

    def _volume_path(self, run_id: str, *parts: str) -> Path:
        """Build a path inside the recordings volume, guarded against traversal."""
        base = Path(self._volume_mount) / run_id
        result = base.joinpath(*parts).resolve()
        if not str(result).startswith(str(base.resolve())):
            raise HTTPException(status_code=400, detail="Invalid path")
        return result

    async def get_manifest(self, run_id: str) -> dict:
        """List recording artifacts from the live sandbox or persisted volume."""
        handle = await self._get_handle(run_id)
        if handle:
            client = self._get_http_client()
            try:
                resp = await client.get(f"{handle.status_base_url}/recording/manifest")
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError:
                log.debug("Manifest proxy failed for run %s", run_id, exc_info=True)

        run_dir = Path(self._volume_mount) / run_id
        if not run_dir.exists():
            raise HTTPException(status_code=404, detail="No recordings found")
        return {"run_id": run_id, "artifacts": scan_recording_artifacts(run_dir)}

    async def get_trace(self, run_id: str) -> Response:
        """Download the Playwright trace ZIP."""
        handle = await self._get_handle(run_id)
        if handle:
            client = self._get_http_client()
            try:

                async def proxy_trace():
                    async with client.stream(
                        "GET",
                        f"{handle.status_base_url}/recording/trace",
                        timeout=None,
                    ) as upstream:
                        upstream.raise_for_status()
                        async for chunk in upstream.aiter_bytes():
                            yield chunk

                return StreamingResponse(
                    proxy_trace(),
                    media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="trace.zip"'},
                )
            except httpx.HTTPError:
                log.debug("Trace proxy failed for run %s", run_id, exc_info=True)

        path = self._volume_path(run_id, "trace.zip")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Trace not available")
        return FileResponse(path, media_type="application/zip", filename="trace.zip")

    async def get_screenshot(self, run_id: str, filename: str) -> FileResponse:
        """Download an individual screenshot from persisted storage."""
        safe_name = Path(filename).name
        path = self._volume_path(run_id, "screenshots", safe_name)
        if not path.exists() or path.suffix != ".jpg":
            raise HTTPException(status_code=404, detail="Screenshot not found")
        return FileResponse(path, media_type="image/jpeg")
