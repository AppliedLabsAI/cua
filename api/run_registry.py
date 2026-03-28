"""Run registry abstraction for API-managed sandbox sessions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RunHandle(BaseModel):
    """Minimal state the API needs to manage an active sandbox run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    sandbox: Any
    status_base_url: str


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
