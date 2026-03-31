"""Run registry abstraction for API-managed sandbox sessions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from api.errors import ApiError


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


class RunRegistry:
    """Abstract storage for active run handles."""

    def add(self, handle: RunHandle) -> None:
        raise NotImplementedError

    def get(self, run_id: str) -> RunHandle | None:
        raise NotImplementedError

    def remove(self, run_id: str) -> RunHandle | None:
        raise NotImplementedError


class InMemoryRunRegistry(RunRegistry):
    """Single-process run registry used by the current API deployment."""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def add(self, handle: RunHandle) -> None:
        self._runs[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    def remove(self, run_id: str) -> RunHandle | None:
        return self._runs.pop(run_id, None)
