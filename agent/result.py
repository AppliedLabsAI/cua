"""Agent result types."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from actionlog.actions import ActionLog
from agent.output import collect_extracted_texts

if TYPE_CHECKING:
    from bridge.router import ActionRouter


class AgentResult(BaseModel):
    """Outcome of a complete agent run."""

    success: bool
    summary: str
    action_count: int
    action_log: list[ActionLog] = Field(default_factory=list)
    total_duration_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    error: str | None = None
    data: dict[str, Any] | None = None
    extracted_texts: list[str] = Field(default_factory=list)
    session_memory: str = ""


def make_error_result(
    error_msg: str,
    *,
    step: int,
    run_start: float,
    bridge: ActionRouter,
    total_input_tokens: int,
    total_output_tokens: int,
    session_memory: str = "",
) -> AgentResult:
    """Build an AgentResult for an error exit."""
    return AgentResult(
        success=False,
        summary="",
        action_count=step,
        action_log=bridge.action_log,
        total_duration_ms=int((time.monotonic() - run_start) * 1000),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        error=error_msg,
        extracted_texts=collect_extracted_texts(bridge.action_log),
        session_memory=session_memory,
    )
