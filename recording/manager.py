"""RecordingManager — facade for all session recording layers."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from recording.config import RecordingConfig
from recording.screenshots import ScreenshotRecorder
from recording.trace import TraceRecorder
from telemetry import get_tracer
from telemetry.spans import RECORDING_START, RECORDING_STOP, RECORDING_UPLOAD

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext

log = logging.getLogger(__name__)


@dataclass
class RecordingArtifact:
    """Metadata for a single recording output file."""

    filename: str
    type: str  # "trace" or "screenshot"
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "type": self.type,
            "size_bytes": self.size_bytes,
        }


@dataclass
class RecordingManifest:
    """Summary of all recording artifacts for a session."""

    run_id: str
    artifacts: list[RecordingArtifact] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }


def scan_recording_artifacts(root: Path) -> list[dict]:
    """Scan a recording directory and return artifact metadata as dicts.

    Shared by api/streaming.py and api/server.py for serving manifests.
    """
    artifacts: list[dict] = []
    trace = root / "trace.zip"
    if trace.exists():
        artifacts.append(
            {
                "filename": "trace.zip",
                "type": "trace",
                "size_bytes": trace.stat().st_size,
            }
        )
    screenshots = root / "screenshots"
    if screenshots.exists():
        for jpg in sorted(screenshots.glob("*.jpg")):
            artifacts.append(
                {
                    "filename": f"screenshots/{jpg.name}",
                    "type": "screenshot",
                    "size_bytes": jpg.stat().st_size,
                }
            )
    return artifacts


class RecordingManager:
    """Unified lifecycle manager for screenshot and trace recording."""

    def __init__(self, config: RecordingConfig, run_id: str) -> None:
        self._config = config
        self._run_id = run_id
        self._screenshots: ScreenshotRecorder | None = None
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
                "recording.screenshots": self._config.screenshots,
                "recording.trace": self._config.trace,
                "recording.output_dir": self._config.output_dir,
            },
        ):
            if self._config.screenshots:
                self._screenshots = ScreenshotRecorder(self._config.output_dir)
                log.info(
                    "Screenshot recording enabled: %s", self._screenshots.directory
                )

            if self._config.trace:
                self._trace = TraceRecorder(self._config.output_dir)
                await self._trace.start(context)

    async def on_screenshot(self, step: int, action: str, screenshot_b64: str) -> None:
        """Persist a screenshot. Called by ActionRouter after each action."""
        if self._screenshots:
            try:
                await self._screenshots.save(step, action, screenshot_b64)
            except Exception as exc:
                log.warning("Failed to save screenshot for step %d: %s", step, exc)

    async def stop(self) -> RecordingManifest:
        """Stop all recorders and return the recording manifest."""
        tracer = get_tracer()
        manifest = RecordingManifest(run_id=self._run_id)

        with tracer.start_as_current_span(RECORDING_STOP) as span:
            # Stop tracing
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

            # Collect screenshot artifacts from in-memory tracking
            if self._screenshots:
                for art_dict in self._screenshots.artifacts:
                    manifest.artifacts.append(
                        RecordingArtifact(
                            filename=art_dict["filename"],
                            type=art_dict["type"],
                            size_bytes=art_dict["size_bytes"],
                        )
                    )

            span.set_attribute("recording.artifact_count", len(manifest.artifacts))
            total_bytes = sum(a.size_bytes for a in manifest.artifacts)
            span.set_attribute("recording.total_bytes", total_bytes)

        log.info(
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
            log.warning("No recording output to upload at %s", src)
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
                log.info("Recordings persisted to %s", dest)
            except Exception as exc:
                span.set_attribute("recording.upload_success", False)
                log.error("Failed to persist recordings: %s", exc)

    @staticmethod
    def _copy_tree(src: Path, dest: Path) -> None:
        """Copy directory tree, creating destination if needed."""
        dest.mkdir(parents=True, exist_ok=True)
        trace_src = src / "trace.zip"
        if trace_src.exists():
            shutil.copy2(trace_src, dest / "trace.zip")
        screenshots_src = src / "screenshots"
        if screenshots_src.exists():
            screenshots_dest = dest / "screenshots"
            if screenshots_dest.exists():
                shutil.rmtree(screenshots_dest)
            shutil.copytree(screenshots_src, screenshots_dest)
