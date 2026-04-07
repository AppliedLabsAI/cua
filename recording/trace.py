"""Playwright tracing wrapper — captures DOM snapshots, network, and console."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext

logger = logging.getLogger(__name__)


class TraceRecorder:
    """Wraps Playwright's built-in tracing API for session replay."""

    def __init__(self, output_dir: str) -> None:
        self._output_path = Path(output_dir) / "trace.zip"
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
            logger.info("Playwright tracing started")
        except Exception as exc:
            logger.warning("Playwright tracing unavailable: %s", exc)
            self._context = None

    async def stop(self) -> str | None:
        """Stop tracing and save to disk. Returns output path or None."""
        if self._context is None:
            return None
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            await self._context.tracing.stop(path=str(self._output_path))
            self._context = None
            logger.info("Trace saved: %s", self._output_path)
            return str(self._output_path)
        except Exception as exc:
            logger.warning("Failed to stop tracing: %s", exc)
            self._context = None
            return None

    @property
    def output_path(self) -> Path:
        return self._output_path

    @property
    def active(self) -> bool:
        return self._context is not None
