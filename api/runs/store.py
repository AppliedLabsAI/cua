"""Persisted run status and replay helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from api.models import RunStatus

logger = logging.getLogger(__name__)


class PersistedRunStore:
    """Read persisted run status and reconstruct replay streams."""

    def __init__(self, *, volume_mount: str, volume) -> None:
        self._volume_mount = volume_mount
        self._volume = volume

    async def load_status(self, run_id: str) -> RunStatus | None:
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

    async def build_event_stream(
        self,
        run_id: str,
        *,
        start_after: int = 0,
    ) -> StreamingResponse | None:
        """Build an SSE response from persisted status on the volume."""
        persisted = await self.load_status(run_id)
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
