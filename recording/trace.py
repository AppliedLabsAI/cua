"""Playwright tracing wrapper — captures DOM snapshots, network, and console."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext

log = logging.getLogger(__name__)


class TraceRecorder:
    """Wraps Playwright's built-in tracing API for session replay."""

    def __init__(self, output_dir: str) -> None:
        self._output_path = Path(output_dir) / "trace.zip"
        self._active = False
        self._context: BrowserContext | None = None

    async def start(self, context: BrowserContext) -> None:
        """Start tracing on the browser context."""
        try:
            await context.tracing.start(
                screenshots=True,
                snapshots=True,
                sources=False,
            )
            self._context = context
            self._active = True
            log.info("Playwright tracing started")
        except Exception as exc:
            log.warning("Playwright tracing unavailable: %s", exc)
            self._active = False

    async def stop(self) -> str | None:
        """Stop tracing and save to disk. Returns output path or None."""
        if not self._active or not self._context:
            return None
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            await self._context.tracing.stop(path=str(self._output_path))
            self._active = False
            log.info("Trace saved: %s", self._output_path)
            return str(self._output_path)
        except Exception as exc:
            log.warning("Failed to stop tracing: %s", exc)
            self._active = False
            return None

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def active(self) -> bool:
        return self._active
