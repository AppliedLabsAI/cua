"""Async background task tracking for bridge side effects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTasks:
    """Track fire-and-forget tasks so callers can drain them on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.warning("Background task failed: %s", task.exception())
