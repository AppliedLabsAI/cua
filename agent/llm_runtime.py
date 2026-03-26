"""LLM transport/runtime helpers for the agent loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic

from actionlog.actions import ActionLog
from agent.context import truncate_tool_result
from bridge.execution import execute_dom_action
from telemetry import (
    execute_tool_with_span,
    finalize_llm_span,
    llm_span_attrs,
    record_text_block,
    record_thinking_block,
)
from telemetry.spans import EVENT_TOOL_SKIPPED, LLM_CALL

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer

    from agent.thinking import AdaptiveThinking
    from bridge import ActionResult
    from bridge.router import ActionRouter

log = logging.getLogger(__name__)

PAGE_CHANGE_ACTIONS = {"goto", "click", "execute_sequence"}
READ_ONLY_ACTIONS = {"extract", "wait_for", "get_dom", "screenshot"}


def are_parallelizable(blocks: list) -> bool:
    """Check if all tool blocks are safe read-only DOM actions."""
    return len(blocks) > 1 and all(
        getattr(block, "name", None) == "browser_dom"
        and (getattr(block, "input", None) or {}).get("action") in READ_ONLY_ACTIONS
        for block in blocks
    )


def skipped_tool_result(block_id: str) -> dict:
    """Build a tool_result for a skipped stale tool call."""
    return {
        "type": "tool_result",
        "tool_use_id": block_id,
        "content": [
            {"type": "text", "text": "Skipped: page changed. Re-observe the DOM."}
        ],
        "is_error": True,
    }


def append_hint_to_last_result(tool_results: list[dict], hint: str) -> None:
    """Append a stuck-detection hint to the last tool result's text content."""
    last_tr = tool_results[-1]
    last_content = last_tr.get("content", [])
    for item in reversed(last_content):
        if isinstance(item, dict) and item.get("type") == "text":
            item["text"] = item["text"] + hint
            return
    last_content.append({"type": "text", "text": hint})


async def streaming_llm_call(
    *,
    client: AsyncAnthropic,
    api_kwargs: Callable[[], dict],
    tracer: Tracer,
    model: str,
    max_tokens: int,
    thinking: AdaptiveThinking,
    bridge: ActionRouter,
    step_base: int,
    iter_span: Span,
    text_parts: list[str],
    on_action: Callable[[ActionLog], None] | None,
) -> tuple[list[dict], list, int, int]:
    """Execute the streaming LLM call path."""
    tool_results: list[dict] = []
    step = step_base
    skip_remaining = False

    with tracer.start_as_current_span(
        LLM_CALL,
        attributes=llm_span_attrs(model, max_tokens, thinking.budget, streaming=True),
    ) as llm_span:
        async with client.beta.messages.stream(**api_kwargs()) as stream:
            async for event in stream:
                if event.type != "content_block_stop":
                    continue
                snapshot = stream.current_message_snapshot
                idx: int = getattr(event, "index", -1)
                if idx >= len(snapshot.content):
                    continue

                block = snapshot.content[idx]
                if block.type == "text":
                    record_text_block(block, llm_span, text_parts)
                    continue
                if block.type == "thinking":
                    record_thinking_block(block, llm_span)
                    continue
                if block.type != "tool_use":
                    continue

                block_name: str = getattr(block, "name", "")
                block_id: str = getattr(block, "id", "")
                block_input: dict[str, Any] = getattr(block, "input", None) or {}
                action = block_input.get("action", "")

                if skip_remaining and action not in READ_ONLY_ACTIONS:
                    log.info("Skipping stale tool call: %s.%s", block_name, action)
                    iter_span.add_event(
                        EVENT_TOOL_SKIPPED,
                        attributes={
                            "tool_name": block_name,
                            "action": action,
                            "reason": "page_changed",
                        },
                    )
                    tool_results.append(skipped_tool_result(block_id))
                    continue

                step += 1
                result = await execute_tool_with_span(
                    tracer,
                    bridge,
                    block_name,
                    block_id,
                    block_input,
                    step,
                )
                is_error = result.get("is_error", False)

                if action in PAGE_CHANGE_ACTIONS:
                    skip_remaining = True
                thinking.record(not is_error)
                if on_action and bridge.action_log:
                    on_action(bridge.action_log[-1])
                tool_results.append(result)

        final = await stream.get_final_message()
        response_content = final.content
        input_tokens = final.usage.input_tokens
        output_tokens = final.usage.output_tokens

        finalize_llm_span(
            llm_span,
            input_tokens,
            output_tokens,
            has_tool_calls=bool(tool_results),
            text_response=text_parts[-1] if text_parts else None,
        )

    return tool_results, response_content, input_tokens, output_tokens


async def fallback_llm_call(
    *,
    client: AsyncAnthropic,
    api_kwargs: Callable[[], dict],
    tracer: Tracer,
    model: str,
    max_tokens: int,
    thinking: AdaptiveThinking,
    bridge: ActionRouter,
    step_base: int,
    text_parts: list[str],
    on_action: Callable[[ActionLog], None] | None,
) -> tuple[list[dict], list, int, int]:
    """Execute the non-streaming fallback LLM call path."""
    with tracer.start_as_current_span(
        LLM_CALL,
        attributes=llm_span_attrs(model, max_tokens, thinking.budget, streaming=False),
    ) as llm_span:
        response = await client.beta.messages.create(**api_kwargs())
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        response_content = response.content

        tool_use_blocks = [
            block for block in response_content if block.type == "tool_use"
        ]
        for block in response_content:
            if block.type == "thinking":
                record_thinking_block(block, llm_span)
            elif block.type == "text":
                record_text_block(block, llm_span, text_parts)

        finalize_llm_span(
            llm_span,
            input_tokens,
            output_tokens,
            has_tool_calls=bool(tool_use_blocks),
            text_response=text_parts[-1] if text_parts else None,
        )

    tool_results: list[dict] = []
    step = step_base

    if are_parallelizable(tool_use_blocks):
        raw_results = await asyncio.gather(
            *[
                _execute_raw_tool_block(
                    action=(block.input or {}).get("action", ""),
                    tool_input=block.input or {},
                    bridge=bridge,
                )
                for block in tool_use_blocks
            ]
        )
        for block, (raw_result, duration_ms) in zip(
            tool_use_blocks, raw_results, strict=False
        ):
            step += 1
            action = (block.input or {}).get("action", "")
            tr = await bridge.build_tool_result_from_raw(
                block.name,
                action,
                block.input or {},
                raw_result,
                duration_ms=duration_ms,
            )
            tr = truncate_tool_result(tr, action)
            is_error = tr.get("is_error", False)
            thinking.record(not is_error)
            if on_action and bridge.action_log:
                on_action(bridge.action_log[-1])
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, **tr})
    else:
        for block in tool_use_blocks:
            step += 1
            result = await execute_tool_with_span(
                tracer,
                bridge,
                block.name,
                block.id,
                block.input or {},
                step,
            )
            is_error = result.get("is_error", False)
            thinking.record(not is_error)
            if on_action and bridge.action_log:
                on_action(bridge.action_log[-1])
            tool_results.append(result)

    return tool_results, response_content, input_tokens, output_tokens


async def _execute_raw_tool_block(
    *,
    action: str,
    tool_input: dict[str, Any],
    bridge: ActionRouter,
) -> tuple[ActionResult, int]:
    start = time.monotonic()
    result = await execute_dom_action(action, tool_input, bridge.browser)
    return result, int((time.monotonic() - start) * 1000)
