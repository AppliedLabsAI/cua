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
    read_only = {"extract", "screenshot", "wait_for"}
    return len(blocks) > 1 and all(
        getattr(b, "name", None) == "browser_dom"
        and (getattr(b, "input", None) or {}).get("action") in read_only
        for b in blocks
    )


async def run_agent(
    directive: str,
    bridge: ActionRouter,
    model: str = "claude-sonnet-4-6",
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

    # Shared API call kwargs (used by both streaming and fallback)
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

    try:
        while step < max_steps:
            # Prune old screenshots and DOM snapshots to keep input tokens down
            prune_old_context(messages, keep_last=1)

            tool_results: list[dict] = []
            response_content = []
            api_call_start = time.monotonic()
            last_input_tokens = 0
            # After a page-changing action, skip remaining tool calls
            # (they were planned on stale state)
            _skip_remaining = False

            try:
                # --- Streaming path: execute tool calls as they arrive ---
                async with client.beta.messages.stream(**_api_kwargs()) as stream:
                    async for event in stream:
                        if event.type == "content_block_stop":
                            snapshot = stream.current_message_snapshot
                            idx: int = getattr(event, "index", -1)
                            if idx < len(snapshot.content):
                                block = snapshot.content[idx]
                                if block.type == "tool_use":
                                    # Cast for ty — discriminated union narrowing
                                    block_name: str = getattr(block, "name", "")
                                    block_id: str = getattr(block, "id", "")
                                    block_input: dict[str, Any] = (
                                        getattr(block, "input", None) or {}
                                    )
                                    action = block_input.get("action", "")
                                    # Allow read-only actions even after
                                    # page change (extract/wait/get_dom are
                                    # safe on the new page state)
                                    if _skip_remaining and action not in READ_ONLY:
                                        log.info(
                                            "Skipping stale tool call: %s.%s",
                                            block_name,
                                            action,
                                        )
                                        tool_results.append(
                                            {
                                                "type": "tool_result",
                                                "tool_use_id": block_id,
                                                "content": [
                                                    {
                                                        "type": "text",
                                                        "text": (
                                                            "Skipped: page changed. "
                                                            "Re-observe the DOM."
                                                        ),
                                                    }
                                                ],
                                                "is_error": True,
                                            }
                                        )
                                        continue
                                    step += 1
                                    tool_result = await bridge.execute(
                                        block_name, block_input
                                    )
                                    tool_result = truncate_tool_result(
                                        tool_result, action
                                    )
                                    # Mark skip if this action changes the page
                                    if action in PAGE_CHANGE_ACTIONS:
                                        _skip_remaining = True
                                    thinking.record(
                                        not tool_result.get("is_error", False)
                                    )
                                    if on_action and bridge.action_log:
                                        on_action(bridge.action_log[-1])
                                    tool_results.append(
                                        {
                                            "type": "tool_result",
                                            "tool_use_id": block_id,
                                            **tool_result,
                                        }
                                    )
                                elif block.type == "text":
                                    text = getattr(block, "text", "") or ""
                                    if text:
                                        text_parts.append(text)
                                        log.info("Agent text: %s", text[:200])
                                elif block.type == "thinking":
                                    thinking_text = getattr(block, "thinking", "") or ""
                                    log.debug("Thinking: %s", thinking_text[:200])

                    final = await stream.get_final_message()
                    response_content = final.content

                last_input_tokens = final.usage.input_tokens
                total_input_tokens += final.usage.input_tokens
                total_output_tokens += final.usage.output_tokens

            except APIError:
                raise
            except Exception as stream_err:
                # --- Fallback: non-streaming with parallel DOM extraction ---
                log.warning("Streaming failed (%s), falling back", stream_err)
                response = await client.beta.messages.create(**_api_kwargs())
                last_input_tokens = response.usage.input_tokens
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                response_content = response.content

                # Separate tool_use blocks from other content
                tool_use_blocks = [b for b in response_content if b.type == "tool_use"]
                for block in response_content:
                    if block.type == "thinking":
                        thinking_text = getattr(block, "thinking", "") or ""
                        log.debug("Thinking: %s", thinking_text[:200])
                    elif block.type == "text":
                        text = getattr(block, "text", "") or ""
                        if text:
                            text_parts.append(text)
                            log.info("Agent text: %s", text[:200])

                if _are_parallelizable(tool_use_blocks):
                    # Parallel: run raw DOM operations, then log sequentially
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
                        # Log via bridge (sequential — safe for shared state)
                        tr = await bridge.build_tool_result_from_raw(
                            block.name, action, block.input, raw
                        )
                        tr = truncate_tool_result(tr, action)
                        thinking.record(not tr.get("is_error", False))
                        if on_action and bridge.action_log:
                            on_action(bridge.action_log[-1])
                        tool_results.append(
                            {"type": "tool_result", "tool_use_id": block.id, **tr}
                        )
                else:
                    for block in tool_use_blocks:
                        step += 1
                        action = (block.input or {}).get("action", "")
                        tool_result = await bridge.execute(block.name, block.input)
                        tool_result = truncate_tool_result(tool_result, action)
                        thinking.record(not tool_result.get("is_error", False))
                        if on_action and bridge.action_log:
                            on_action(bridge.action_log[-1])
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                **tool_result,
                            }
                        )

            api_ms = int((time.monotonic() - api_call_start) * 1000)
            log.info(
                "API call: %dms, %d tool calls, tokens: %d in",
                api_ms,
                len(tool_results),
                last_input_tokens,
            )

            messages.append({"role": "assistant", "content": response_content})

            if not tool_results:
                log.info("Agent finished (no tool calls)")
                break

            # --- Stuck detection ---
            stuck_detector.record(tool_results)
            hint = stuck_detector.get_hint()
            if hint and tool_results:
                # Append to the last tool_result's text content
                last_tr = tool_results[-1]
                last_content = last_tr.get("content", [])
                appended = False
                for item in reversed(last_content):
                    if isinstance(item, dict) and item.get("type") == "text":
                        item["text"] = item["text"] + hint
                        appended = True
                        break
                if not appended:
                    last_content.append({"type": "text", "text": hint})

            messages.append({"role": "user", "content": tool_results})

        else:
            log.warning("Agent hit max_steps limit (%d)", max_steps)

    except APIError as e:
        log.error("Anthropic API error: %s", e)
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
