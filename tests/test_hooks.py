"""Tests for pydantic-ai hooks (agent/hooks.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.exceptions import SkipToolExecution
from pydantic_ai.messages import (
    ModelResponse,
    ThinkingPart,
    ToolCallPart,
)

from actionlog.actions import ActionLog
from agent.hooks import (
    attach_thinking_to_action_log,
    build_hooks,
    capture_thinking,
    handle_tool_error,
    preflight_guardrail,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_ctx(
    *,
    step: int = 0,
    action_log: list | None = None,
    last_thinking: str | None = None,
    guardrail_block: str | None = None,
) -> MagicMock:
    """Create a minimal mock RunContext with deps.

    Args:
        guardrail_block: If set, bridge.check_guardrails() returns this reason.
    """
    deps = MagicMock()
    deps.step = step
    deps.last_thinking = last_thinking
    deps.bridge.action_log = action_log if action_log is not None else []
    deps.bridge.check_guardrails = AsyncMock(return_value=guardrail_block)

    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _make_response(*parts) -> ModelResponse:
    return ModelResponse(parts=list(parts), timestamp=datetime.now(UTC))


def _make_action_log_entry(thinking: str | None = None) -> ActionLog:
    return ActionLog(
        step=1,
        timestamp=datetime.now(UTC).isoformat(),
        tool="browser_dom",
        action="click",
        input_summary="click '#btn'",
        tool_input={"action": "click", "selector": "#btn"},
        duration_ms=100,
        success=True,
        thinking=thinking,
    )


# ── build_hooks ─────────────────────────────────────────────────────


def test_build_hooks_returns_hooks_instance():
    assert isinstance(build_hooks(), Hooks)


# ── capture_thinking ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_thinking_extracts_thinking_part():
    ctx = _make_ctx()
    response = _make_response(
        ThinkingPart(content="I should click the login button"),
        ToolCallPart(tool_name="browser_dom", args='{"action": "click"}'),
    )

    result = await capture_thinking(ctx, request_context=MagicMock(), response=response)

    assert result is response
    assert ctx.deps.last_thinking == "I should click the login button"


@pytest.mark.asyncio
async def test_capture_thinking_truncates_long_thinking():
    ctx = _make_ctx()
    long_thinking = "x" * 5000
    response = _make_response(
        ThinkingPart(content=long_thinking),
        ToolCallPart(tool_name="browser_dom", args="{}"),
    )

    await capture_thinking(ctx, request_context=MagicMock(), response=response)

    assert ctx.deps.last_thinking is not None
    assert len(ctx.deps.last_thinking) == 2000
    assert ctx.deps.last_thinking == long_thinking[-2000:]


@pytest.mark.asyncio
async def test_capture_thinking_no_thinking_parts():
    ctx = _make_ctx()
    response = _make_response(
        ToolCallPart(tool_name="browser_dom", args="{}"),
    )

    await capture_thinking(ctx, request_context=MagicMock(), response=response)

    assert ctx.deps.last_thinking is None


# ── attach_thinking_to_action_log ───────────────────────────────────


@pytest.mark.asyncio
async def test_attach_thinking_fills_empty_action_log():
    entry = _make_action_log_entry(thinking=None)
    ctx = _make_ctx(last_thinking="Navigating to admin", action_log=[entry])

    result = await attach_thinking_to_action_log(
        ctx, call=MagicMock(), tool_def=MagicMock(), args={}, result="ok"
    )

    assert entry.thinking == "Navigating to admin"
    assert ctx.deps.last_thinking is None
    assert result == "ok"


@pytest.mark.asyncio
async def test_attach_thinking_does_not_overwrite_existing():
    entry = _make_action_log_entry(thinking="Already has reasoning")
    ctx = _make_ctx(last_thinking="New thinking", action_log=[entry])

    await attach_thinking_to_action_log(
        ctx, call=MagicMock(), tool_def=MagicMock(), args={}, result=None
    )

    assert entry.thinking == "Already has reasoning"


@pytest.mark.asyncio
async def test_attach_thinking_clears_last_thinking():
    ctx = _make_ctx(last_thinking="Some thinking", action_log=[])

    await attach_thinking_to_action_log(
        ctx, call=MagicMock(), tool_def=MagicMock(), args={}, result=None
    )

    assert ctx.deps.last_thinking is None


# ── preflight_guardrail ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preflight_allows_when_guardrails_pass():
    ctx = _make_ctx(guardrail_block=None)
    args = {"action": "click", "selector": "#btn"}

    result = await preflight_guardrail(
        ctx, call=MagicMock(), tool_def=MagicMock(), args=args
    )

    assert result == args
    ctx.deps.bridge.check_guardrails.assert_awaited_once_with("browser_dom", args)


@pytest.mark.asyncio
async def test_preflight_blocks_when_guardrails_reject():
    ctx = _make_ctx(guardrail_block="Action 'click' not in allowed scope")

    with pytest.raises(SkipToolExecution):
        await preflight_guardrail(
            ctx,
            call=MagicMock(),
            tool_def=MagicMock(),
            args={"action": "click", "selector": "#btn"},
        )


# ── handle_tool_error ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_error_returns_structured_error():
    ctx = _make_ctx(step=3)

    result = await handle_tool_error(
        ctx,
        call=MagicMock(),
        tool_def=MagicMock(),
        args={"action": "click"},
        error=RuntimeError("Element detached"),
    )

    assert "Element detached" in result.return_value
    assert result.metadata["is_error"] is True
    assert result.metadata["action"] == "click"
