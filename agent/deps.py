"""Runtime dependencies for the CUA Pydantic AI agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from actionlog.actions import ActionLog
    from bridge.router import ActionRouter
    from credentials import SecretValue


@dataclass
class AgentDeps:
    """Carries runtime state through Pydantic AI's dependency injection."""

    bridge: ActionRouter
    directive: str
    max_steps: int = 50
    credentials: dict[str, SecretValue] | None = None
    profile_prompt: str | None = None
    output_schema: dict[str, Any] | None = None
    on_action: Callable[[ActionLog], None] | None = None
    allowed_actions: frozenset[str] | None = None

    # Mutable counters — used by loop.py error path and post-extraction accumulation.
    step: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Set by after_model_request hook; consumed by after_tool_execute hook.
    last_thinking: str | None = None
