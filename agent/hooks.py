"""Pydantic AI hooks for the CUA agent.

Registers lifecycle hooks on the agent for observability, pre-flight
guardrails, and error recovery. All hooks are assembled via build_hooks()
and passed to the Agent constructor as capabilities=[build_hooks()].

Hook functions are defined at module level for testability, then
registered inside build_hooks().
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext, ToolDefinition, ToolReturn
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.exceptions import SkipToolExecution
from pydantic_ai.messages import ModelResponse

if TYPE_CHECKING:
    from pydantic_ai._agent_graph import ModelRequestContext
    from pydantic_ai.messages import ToolCallPart

    from agent.deps import AgentDeps

logger = logging.getLogger(__name__)

# Max chars of thinking to persist in ActionLog
_MAX_THINKING_STORE = 2000


# ── Phase 1: Observability ──────────────────────────────────────────


async def capture_thinking(
    ctx: RunContext[AgentDeps],
    *,
    request_context: ModelRequestContext,
    response: ModelResponse,
) -> ModelResponse:
    """Extract thinking from model response and stash on deps for the tool hook."""
    thinking = response.thinking
    if thinking:
        if len(thinking) > _MAX_THINKING_STORE:
            thinking = thinking[-_MAX_THINKING_STORE:]
        ctx.deps.last_thinking = thinking

    return response


async def attach_thinking_to_action_log(
    ctx: RunContext[AgentDeps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: dict[str, Any],
    result: Any,
) -> Any:
    """If the tool's reasoning param was empty, fill ActionLog.thinking from model thinking."""
    deps = ctx.deps
    if deps.last_thinking and deps.bridge.action_log:
        entry = deps.bridge.action_log[-1]
        if not entry.thinking:
            entry.thinking = deps.last_thinking
    # Clear after use so it doesn't leak to the next step
    deps.last_thinking = None
    return result


# ── Phase 2: Pre-Flight Guardrails ──────────────────────────────────


async def preflight_guardrail(
    ctx: RunContext[AgentDeps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run guardrail checks before the tool function executes.

    Delegates to ActionRouter.check_guardrails() which runs the full
    guardrail suite (ScopeVerifier, destructive-click policy, SSRF,
    domain blocking, navigation limits) with OTel spans and metrics.

    Raises SkipToolExecution to block disallowed actions without entering
    the tool function.
    """
    block_reason = await ctx.deps.bridge.check_guardrails("browser_dom", args)
    if block_reason:
        logger.warning("Guardrail blocked: %s", block_reason)
        raise SkipToolExecution(
            ToolReturn(
                return_value=f"Error: Guardrail blocked: {block_reason}",
            )
        )
    return args


# ── Phase 2B: Error Recovery ────────────────────────────────────────


async def handle_tool_error(
    ctx: RunContext[AgentDeps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: dict[str, Any],
    error: Exception,
) -> Any:
    """Catch unhandled tool exceptions and return a structured error."""
    action = args.get("action", "")
    logger.error(
        "Unhandled tool error (step %d, %s): %s",
        ctx.deps.step,
        action,
        error,
        exc_info=error,
    )
    return ToolReturn(
        return_value=f"Error: {action} failed unexpectedly: {error}",
        metadata={"action": action, "step": ctx.deps.step, "is_error": True},
    )


# ── Factory ─────────────────────────────────────────────────────────


def build_hooks() -> Hooks[AgentDeps]:
    """Build the Hooks capability for the CUA agent."""
    hooks: Hooks[AgentDeps] = Hooks()
    hooks.on.after_model_request(capture_thinking)
    hooks.on.after_tool_execute(attach_thinking_to_action_log, tools=["browser_dom"])
    hooks.on.before_tool_execute(preflight_guardrail, tools=["browser_dom"])
    hooks.on.tool_execute_error(handle_tool_error, tools=["browser_dom"])
    return hooks
