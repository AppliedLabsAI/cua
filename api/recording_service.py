"""Recording artifact services for live and completed runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import modal
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from starlette.responses import Response

from api.run_registry import RunHandle
from recording.manager import scan_recording_artifacts

logger = logging.getLogger(__name__)


class RecordingService:
    """Serve recording artifacts from a live sandbox or persisted volume."""

    def __init__(
        self,
        *,
        volume_mount: str,
        volume: modal.Volume,
        get_http_client: Callable[[], httpx.AsyncClient],
        get_handle: Callable[[str], Awaitable[RunHandle | None]],
    ) -> None:
        self._volume_mount = volume_mount
        self._volume = volume
        self._get_http_client = get_http_client
        self._get_handle = get_handle

    def _volume_path(self, run_id: str, *parts: str) -> Path:
        """Build a path inside the recordings volume, guarded against traversal."""
        base = (Path(self._volume_mount) / run_id).resolve()
        result = base.joinpath(*parts).resolve()
        if not result.is_relative_to(base):
            raise HTTPException(status_code=400, detail="Invalid path")
        return result

    async def _reload_volume(self) -> None:
        try:
            await self._volume.reload.aio()
        except Exception:
            logger.debug("Volume reload failed", exc_info=True)

    async def _trace_path(self, run_id: str) -> Path:
        await self._reload_volume()
        return self._volume_path(run_id, "trace.zip")

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
                logger.debug("Manifest proxy failed for run %s", run_id, exc_info=True)

        await self._reload_volume()
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
                upstream = await client.send(
                    client.build_request(
                        "GET",
                        f"{handle.status_base_url}/recording/trace",
                    ),
                    stream=True,
                )
                upstream.raise_for_status()

                async def proxy_trace():
                    try:
                        async for chunk in upstream.aiter_bytes():
                            yield chunk
                    except httpx.HTTPError as exc:
                        logger.warning(
                            "Trace stream failed for run %s: %s",
                            run_id,
                            exc,
                            exc_info=True,
                        )
                        path = await self._trace_path(run_id)
                        if path.exists():
                            with path.open("rb") as fallback:
                                while True:
                                    chunk = await asyncio.to_thread(
                                        fallback.read, 64 * 1024
                                    )
                                    if not chunk:
                                        break
                                    yield chunk
                    finally:
                        await upstream.aclose()

                return StreamingResponse(
                    proxy_trace(),
                    media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="trace.zip"'},
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Trace proxy failed for run %s: %s",
                    run_id,
                    exc,
                    exc_info=True,
                )
                if "upstream" in locals():
                    await upstream.aclose()

        path = await self._trace_path(run_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Trace not available")
        return FileResponse(path, media_type="application/zip", filename="trace.zip")
