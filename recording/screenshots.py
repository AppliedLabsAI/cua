"""Screenshot persistence — saves per-action JPEGs to disk."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from actionlog.actions import _sanitize_filename
from recording.models import RecordingArtifact

log = logging.getLogger(__name__)


class ScreenshotRecorder:
    """Persists base64 JPEG screenshots to disk at each action step."""

    def __init__(self, output_dir: str) -> None:
        self._dir = Path(output_dir) / "screenshots"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._artifacts: list[RecordingArtifact] = []

    async def save(self, step: int, action: str, screenshot_b64: str) -> Path:
        """Decode base64 JPEG and write to disk. Runs in a thread."""
        path = self._dir / f"{step:04d}_{_sanitize_filename(action)}.jpg"
        await asyncio.to_thread(self._write, path, screenshot_b64)
        size = path.stat().st_size
        self._artifacts.append(
            RecordingArtifact(
                filename=f"screenshots/{path.name}",
                type="screenshot",
                size_bytes=size,
            )
        )
        log.debug("Saved screenshot: %s (%d bytes)", path.name, size)
        return path

    @staticmethod
    def _write(path: Path, b64: str) -> None:
        path.write_bytes(base64.b64decode(b64))

    @property
    def artifacts(self) -> list[RecordingArtifact]:
        return list(self._artifacts)

    @property
    def directory(self) -> Path:
        return self._dir
