"""Run registry abstraction for API-managed sandbox sessions.

InMemoryRunRegistry: local dev (single process).
ModalDictRunRegistry: production (shared across Modal containers via modal.Dict).
"""

from __future__ import annotations

import contextlib
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from api.errors import ApiError

logger = logging.getLogger(__name__)


class RunPhase(StrEnum):
    """Lifecycle phase of a sandbox as tracked by the outer API."""

    RUNNING = "running"
    TERMINATED = "terminated"
    FAILED = "failed"


class RunHandle(BaseModel):
    """Minimal state the API needs to manage an active sandbox run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    sandbox: Any
    status_base_url: str
    phase: RunPhase = RunPhase.RUNNING
    error: ApiError | None = None


_SERIALIZABLE_FIELDS = {"run_id", "status_base_url", "phase"}


class RunRegistry:
    """Abstract storage for active run handles."""

    def add(self, handle: RunHandle) -> None:
        raise NotImplementedError

    def get(self, run_id: str) -> RunHandle | None:
        raise NotImplementedError

    def remove(self, run_id: str) -> RunHandle | None:
        raise NotImplementedError

    def contains(self, run_id: str) -> bool:
        raise NotImplementedError


class InMemoryRunRegistry(RunRegistry):
    """Single-process run registry for local development."""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def add(self, handle: RunHandle) -> None:
        self._runs[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> RunHandle | None:
        return self._runs.pop(run_id, None)

    def contains(self, run_id: str) -> bool:
        return run_id in self._runs


class ModalDictRunRegistry(RunRegistry):
    """Cross-container run registry backed by modal.Dict.

    Stores serializable run metadata in the shared dict. The full
    RunHandle (including the non-serializable modal.Sandbox object) is
    cached locally; on cache miss, RunService.get_handle reconstructs
    it via modal.Sandbox.from_id().
    """

    def __init__(self, modal_dict: Any) -> None:
        self._dict = modal_dict
        self._local: dict[str, RunHandle] = {}

    def add(self, handle: RunHandle) -> None:
        self._local[handle.run_id] = handle
        try:
            self._dict.put(
                handle.run_id,
                handle.model_dump(include=_SERIALIZABLE_FIELDS),
            )
        except Exception:
            logger.warning(
                "Failed to persist run %s to modal.Dict", handle.run_id, exc_info=True
            )

    def get(self, run_id: str) -> RunHandle | None:
        # Local cache only — on miss, RunService.get_handle reconstructs
        # via modal.Sandbox.from_id() and re-adds to the registry.
        return self._local.get(run_id)

    def remove(self, run_id: str) -> RunHandle | None:
        handle = self._local.pop(run_id, None)
        with contextlib.suppress(Exception):
            self._dict.pop(run_id)
        return handle

    def contains(self, run_id: str) -> bool:
        """Check if a run_id exists in the shared registry."""
        if run_id in self._local:
            return True
        try:
            return self._dict.contains(run_id)
        except Exception:
            return False
