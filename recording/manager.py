"""RecordingManager — facade for session recording (trace only)."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from recording.config import RecordingConfig
from recording.models import (
    RecordingArtifact,
    RecordingManifest,
    save_recording_manifest,
)
from recording.trace import TraceRecorder
from telemetry import get_tracer
from telemetry.spans import RECORDING_START, RECORDING_STOP, RECORDING_UPLOAD

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext

logger = logging.getLogger(__name__)


class RecordingManager:
    """Unified lifecycle manager for trace recording."""

    def __init__(self, config: RecordingConfig, run_id: str) -> None:
        self._config = config
        self._run_id = run_id
        self._trace: TraceRecorder | None = None

    @property
    def output_dir(self) -> str:
        return self._config.output_dir

    async def start(self, context: BrowserContext) -> None:
        """Initialize enabled recorders. Call after browser launch."""
        tracer = get_tracer()
        with tracer.start_as_current_span(
            RECORDING_START,
            attributes={
                "recording.trace": self._config.trace,
                "recording.output_dir": self._config.output_dir,
            },
        ):
            if self._config.trace:
                self._trace = TraceRecorder(self._config.output_dir)
                await self._trace.start(context)

    async def stop(self) -> RecordingManifest:
        """Stop all recorders and return the recording manifest."""
        tracer = get_tracer()
        manifest = RecordingManifest(run_id=self._run_id)

        with tracer.start_as_current_span(RECORDING_STOP) as span:
            if self._trace:
                trace_path_str = await self._trace.stop()
                if trace_path_str:
                    trace_path = Path(trace_path_str)
                    manifest.artifacts.append(
                        RecordingArtifact(
                            filename=trace_path.name,
                            type="trace",
                            size_bytes=trace_path.stat().st_size,
                        )
                    )

            span.set_attribute("recording.artifact_count", len(manifest.artifacts))
            total_bytes = sum(a.size_bytes for a in manifest.artifacts)
            span.set_attribute("recording.total_bytes", total_bytes)

        save_recording_manifest(Path(self._config.output_dir), manifest)
        logger.info(
            "Recording stopped: %d artifacts (%d bytes) for run %s",
            len(manifest.artifacts),
            total_bytes,
            self._run_id,
        )
        return manifest

    async def upload(self, volume_path: str) -> None:
        """Copy recording artifacts to a persistent location."""
        src = Path(self._config.output_dir)
        dest = Path(volume_path)

        if not src.exists():
            logger.warning("No recording output to upload at %s", src)
            return

        tracer = get_tracer()
        with tracer.start_as_current_span(
            RECORDING_UPLOAD,
            attributes={
                "recording.src": str(src),
                "recording.dest": str(dest),
            },
        ) as span:
            try:
                await asyncio.to_thread(self._copy_tree, src, dest)
                span.set_attribute("recording.upload_success", True)
                logger.info("Recordings persisted to %s", dest)
            except Exception as exc:
                span.set_attribute("recording.upload_success", False)
                logger.error("Failed to persist recordings: %s", exc)

    @staticmethod
    def _copy_tree(src: Path, dest: Path) -> None:
        """Copy directory tree, creating destination if needed."""
        dest.mkdir(parents=True, exist_ok=True)
        trace_src = src / "trace.zip"
        if trace_src.exists():
            shutil.copy2(trace_src, dest / "trace.zip")
        manifest_src = src / "manifest.json"
        if manifest_src.exists():
            shutil.copy2(manifest_src, dest / "manifest.json")
