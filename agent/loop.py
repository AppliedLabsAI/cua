"""Main agent loop.

Orchestrates the CUA cycle: send screenshots/context to Claude, receive tool
calls, execute via ActionRouter, repeat until done or max_steps reached.

Uses streaming to execute tool calls as they arrive. Falls back to non-streaming
on error. Includes context management to prevent conversation bloat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from anthropic import APIError, AsyncAnthropic

from actionlog.actions import ActionLog
from agent.context import prune_old_context, truncate_tool_result
from agent.prompts import build_system_prompt
from agent.result import AgentResult, make_error_result
from agent.stuck import StuckDetector
from agent.thinking import AdaptiveThinking
from agent.tools import get_tools
from bridge import DOM_MARKER
from bridge.router import ActionRouter
from settings import AGENT_MODEL
from telemetry import (
    execute_tool_with_span,
    finalize_llm_span,
    get_tracer,
    llm_span_attrs,
    record_text_block,
    record_thinking_block,
)
from telemetry.metrics import (
    errors_total,
    iteration_duration,
    llm_call_duration,
    llm_calls_total,
    llm_tokens_input,
    llm_tokens_output,
)
from telemetry.spans import (
    AGENT_ITERATION,
    ATTR_ITER_NUMBER,
    ATTR_ITER_STREAMING,
    ATTR_ITER_THINKING_BUDGET,
    ATTR_ITER_TOOL_CALLS,
    EVENT_STUCK,
    EVENT_TOOL_SKIPPED,
    LLM_CALL,
)

log = logging.getLogger(__name__)

_BETA_FLAGS = ["interleaved-thinking-2025-05-14"]
_MAX_TOKENS = 2048

PAGE_CHANGE_ACTIONS = {"goto", "click", "execute_sequence"}
READ_ONLY = {
    "extract",
    "wait_for",
    "get_dom",
    "screenshot",
}


def _are_parallelizable(blocks: list) -> bool:
    """Check if all tool_use blocks are read-only DOM actions (safe to parallelize)."""
    return len(blocks) > 1 and all(
        getattr(b, "name", None) == "browser_dom"
        and (getattr(b, "input", None) or {}).get("action") in READ_ONLY
        for b in blocks
    )


def _skipped_tool_result(block_id: str) -> dict:
    """Build a tool_result for a skipped (stale) tool call."""
    return {
        "type": "tool_result",
        "tool_use_id": block_id,
        "content": [
            {"type": "text", "text": "Skipped: page changed. Re-observe the DOM."}
        ],
        "is_error": True,
    }


def _record_llm_metrics(
    api_ms: int,
    model: str,
    is_streaming: bool,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record LLM call metrics in one place."""
    labels = {"model": model, "streaming": is_streaming}
    llm_calls_total().add(1, labels)
    llm_call_duration().record(api_ms, labels)
    llm_tokens_input().record(input_tokens, {"model": model})
    llm_tokens_output().record(output_tokens, {"model": model})


async def run_agent(
    directive: str,
    bridge: ActionRouter,
    model: str = AGENT_MODEL,
    max_steps: int = 50,
    thinking_budget: int = 4096,
    credentials: dict | None = None,
    on_action: Callable[[ActionLog], None] | None = None,
    client: AsyncAnthropic | None = None,
    profile_prompt: str | None = None,
    allowed_actions: frozenset[str] | None = None,
) -> AgentResult:
    """Run the CUA agent loop with streaming, context management, and adaptive thinking."""
    run_start = time.monotonic()
    client = client or AsyncAnthropic()
    thinking = AdaptiveThinking(
        base=thinking_budget, reduced=max(1024, thinking_budget // 4)
    )
    stuck_detector = StuckDetector()

    system_prompt = build_system_prompt(
        directive=directive,
        credentials=credentials,
        profile_prompt=profile_prompt,
    )
    tools = get_tools(allowed_actions=allowed_actions)
    system = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Skip initial screenshot — the agent's first goto/click returns one anyway,
    # so an upfront screenshot just wastes ~1500 image tokens.
    # Only include initial DOM if browser is already on a page (for start_url flows).
    page_url = bridge.browser.page.url
    if page_url and page_url != "about:blank":
        from bridge.browser import quick_dom_snapshot

        dom = await quick_dom_snapshot(
            bridge.browser.page,
            filter_config=getattr(bridge, "_filter_config", None),
        )
        if dom:
            initial_content = [
                {
                    "type": "text",
                    "text": f"Current page:\n{DOM_MARKER}\n{dom}\n\n{directive}",
                }
            ]
        else:
            initial_content = [{"type": "text", "text": directive}]
    else:
        initial_content = [{"type": "text", "text": directive}]

    messages: list[dict] = [{"role": "user", "content": initial_content}]

    total_input_tokens = 0
    total_output_tokens = 0
    text_parts: list[str] = []
    step = 0

    def _api_kwargs() -> dict:
        return {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "tools": tools,
            "messages": messages,
            "system": system,
            "betas": _BETA_FLAGS,
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking.budget,
            },
        }

    tracer = get_tracer()
    iteration_num = 0

    try:
        while step < max_steps:
            iteration_num += 1
            iter_start = time.monotonic()

            with tracer.start_as_current_span(
                AGENT_ITERATION,
                attributes={
                    ATTR_ITER_NUMBER: iteration_num,
                    ATTR_ITER_THINKING_BUDGET: thinking.budget,
                },
            ) as iter_span:
                prune_old_context(messages, keep_last=1)

                tool_results: list[dict] = []
                response_content = []
                api_call_start = time.monotonic()
                last_input_tokens = 0
                last_output_tokens = 0
                _skip_remaining = False
                is_streaming = True

                try:
                    (
                        tool_results,
                        response_content,
                        last_input_tokens,
                        last_output_tokens,
                    ) = await _streaming_llm_call(
                        client,
                        _api_kwargs,
                        tracer,
                        model,
                        thinking,
                        bridge,
                        step,
                        iter_span,
                        text_parts,
                        on_action,
                    )
                    step += len(tool_results)
                except APIError:
                    raise
                except Exception as stream_err:
                    is_streaming = False
                    log.warning("Streaming failed (%s), falling back", stream_err)
                    (
                        tool_results,
                        response_content,
                        last_input_tokens,
                        last_output_tokens,
                    ) = await _fallback_llm_call(
                        client,
                        _api_kwargs,
                        tracer,
                        model,
                        thinking,
                        bridge,
                        step,
                        text_parts,
                        on_action,
                    )
                    step += len(tool_results)

                total_input_tokens += last_input_tokens
                total_output_tokens += last_output_tokens

                api_ms = int((time.monotonic() - api_call_start) * 1000)
                log.info(
                    "API call: %dms, %d tool calls, tokens: %d in",
                    api_ms,
                    len(tool_results),
                    last_input_tokens,
                )
                _record_llm_metrics(
                    api_ms, model, is_streaming, last_input_tokens, last_output_tokens
                )

                messages.append({"role": "assistant", "content": response_content})

                if not tool_results:
                    log.info("Agent finished (no tool calls)")
                    iter_span.set_attributes(
                        {
                            ATTR_ITER_TOOL_CALLS: 0,
                            ATTR_ITER_STREAMING: is_streaming,
                        }
                    )
                    break

                # --- Stuck detection ---
                stuck_detector.record(tool_results)
                hint = stuck_detector.get_hint()
                if hint and tool_results:
                    iter_span.add_event(EVENT_STUCK, attributes={"hint": hint[:200]})
                    _append_hint_to_last_result(tool_results, hint)

                messages.append({"role": "user", "content": tool_results})

                iter_span.set_attributes(
                    {
                        ATTR_ITER_TOOL_CALLS: len(tool_results),
                        ATTR_ITER_STREAMING: is_streaming,
                    }
                )
                iteration_duration().record(int((time.monotonic() - iter_start) * 1000))

        else:
            log.warning("Agent hit max_steps limit (%d)", max_steps)

    except APIError as e:
        log.error("Anthropic API error: %s", e)
        errors_total().add(1, {"component": "agent", "error_type": "api_error"})
        return make_error_result(
            f"API error: {e.message}",
            step=step,
            run_start=run_start,
            bridge=bridge,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )
    except Exception as e:
        log.error("Agent loop error: %s", e, exc_info=True)
        errors_total().add(1, {"component": "agent", "error_type": "loop_error"})
        return make_error_result(
            str(e),
            step=step,
            run_start=run_start,
            bridge=bridge,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )

    summary = "\n".join(text_parts) if text_parts else f"Completed in {step} steps"

    return AgentResult(
        success=True,
        summary=summary,
        action_count=step,
        action_log=bridge.action_log,
        total_duration_ms=int((time.monotonic() - run_start) * 1000),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
    )


# ---------------------------------------------------------------------------
# Extracted helpers — reduce nesting and eliminate duplication
# ---------------------------------------------------------------------------


def _append_hint_to_last_result(tool_results: list[dict], hint: str) -> None:
    """Append a stuck-detection hint to the last tool result's text content."""
    last_tr = tool_results[-1]
    last_content = last_tr.get("content", [])
    for item in reversed(last_content):
        if isinstance(item, dict) and item.get("type") == "text":
            item["text"] = item["text"] + hint
            return
    last_content.append({"type": "text", "text": hint})


async def _streaming_llm_call(
    client: AsyncAnthropic,
    api_kwargs: Callable[[], dict],
    tracer: Any,
    model: str,
    thinking: AdaptiveThinking,
    bridge: ActionRouter,
    step_base: int,
    iter_span: Any,
    text_parts: list[str],
    on_action: Callable[[ActionLog], None] | None,
) -> tuple[list[dict], list, int, int]:
    """Execute the streaming LLM call path.

    Returns (tool_results, response_content, input_tokens, output_tokens).
    """
    tool_results: list[dict] = []
    step = step_base
    _skip_remaining = False

    with tracer.start_as_current_span(
        LLM_CALL,
        attributes=llm_span_attrs(model, _MAX_TOKENS, thinking.budget, streaming=True),
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
                elif block.type == "thinking":
                    record_thinking_block(block, llm_span)
                elif block.type == "tool_use":
                    block_name: str = getattr(block, "name", "")
                    block_id: str = getattr(block, "id", "")
                    block_input: dict[str, Any] = getattr(block, "input", None) or {}
                    action = block_input.get("action", "")

                    if _skip_remaining and action not in READ_ONLY:
                        log.info("Skipping stale tool call: %s.%s", block_name, action)
                        iter_span.add_event(
                            EVENT_TOOL_SKIPPED,
                            attributes={
                                "tool_name": block_name,
                                "action": action,
                                "reason": "page_changed",
                            },
                        )
                        tool_results.append(_skipped_tool_result(block_id))
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
                    tool_result_data = {
                        k: v
                        for k, v in result.items()
                        if k not in ("type", "tool_use_id")
                    }
                    is_error = tool_result_data.get("is_error", False)

                    if action in PAGE_CHANGE_ACTIONS:
                        _skip_remaining = True
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
            has_tool_calls=len(tool_results) > 0,
            text_response=text_parts[-1] if text_parts else None,
        )

    return tool_results, response_content, input_tokens, output_tokens


async def _fallback_llm_call(
    client: AsyncAnthropic,
    api_kwargs: Callable[[], dict],
    tracer: Any,
    model: str,
    thinking: AdaptiveThinking,
    bridge: ActionRouter,
    step_base: int,
    text_parts: list[str],
    on_action: Callable[[ActionLog], None] | None,
) -> tuple[list[dict], list, int, int]:
    """Execute the non-streaming fallback LLM call path.

    Returns (tool_results, response_content, input_tokens, output_tokens).
    """
    with tracer.start_as_current_span(
        LLM_CALL,
        attributes=llm_span_attrs(model, _MAX_TOKENS, thinking.budget, streaming=False),
    ) as llm_span:
        response = await client.beta.messages.create(**api_kwargs())
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        response_content = response.content

        tool_use_blocks = [b for b in response_content if b.type == "tool_use"]
        for block in response_content:
            if block.type == "thinking":
                record_thinking_block(block, llm_span)
            elif block.type == "text":
                record_text_block(block, llm_span, text_parts)

        finalize_llm_span(
            llm_span,
            input_tokens,
            output_tokens,
            has_tool_calls=len(tool_use_blocks) > 0,
            text_response=text_parts[-1] if text_parts else None,
        )

    tool_results: list[dict] = []
    step = step_base

    if _are_parallelizable(tool_use_blocks):
        from bridge.browser import execute_dom_action

        raw_results = await asyncio.gather(
            *[
                execute_dom_action(
                    (b.input or {}).get("action", ""),
                    b.input or {},
                    bridge.browser,
                )
                for b in tool_use_blocks
            ]
        )
        for block, raw in zip(tool_use_blocks, raw_results, strict=False):
            step += 1
            action = (block.input or {}).get("action", "")
            tr = await bridge.build_tool_result_from_raw(
                block.name, action, block.input, raw
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
