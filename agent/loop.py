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
from dataclasses import dataclass, field
from typing import Any, cast

from anthropic import APIError, AsyncAnthropic

from actionlog.actions import ActionLog
from agent.prompts import build_system_prompt
from agent.tools import get_tools
from bridge import DOM_MARKER
from bridge.router import ActionRouter

log = logging.getLogger(__name__)

_BETA_FLAGS = ["interleaved-thinking-2025-05-14"]
_MAX_TOKENS = 2048
# Tool result text longer than this is truncated before sending back to Claude.
_MAX_RESULT_CHARS = 2000

MAX_RECENT = 6  # check last N actions for repetition
PAGE_CHANGE_ACTIONS = {"goto", "click", "execute_sequence"}
READ_ONLY = {
    "extract",
    "wait_for",
    "get_dom",
    "screenshot",
}


@dataclass(slots=True)
class AgentResult:
    """Outcome of a complete agent run."""

    success: bool
    summary: str
    action_count: int
    action_log: list[ActionLog] = field(default_factory=list)
    total_duration_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    error: str | None = None


class _AdaptiveThinking:
    """Scale thinking budget based on consecutive successes and step count.

    Early steps get full budget for planning. After consecutive successes,
    budget drops since the agent is in a known-good execution flow.
    Errors reset to full budget for recovery reasoning.
    """

    def __init__(self, base: int = 1024, reduced: int = 1024) -> None:
        self.base = base
        self.reduced = reduced
        self._successes = 0
        self._step = 0

    @property
    def budget(self) -> int:
        self._step += 1
        # First 2 steps: full budget for task planning
        if self._step <= 2:
            return self.base
        # After 3+ consecutive successes: reduced budget
        if self._successes >= 3:
            return self.reduced
        return self.base

    def record(self, success: bool) -> None:
        if success:
            self._successes = min(self._successes + 1, 5)
        else:
            self._successes = 0


def _prune_old_context(messages: list[dict[str, Any]], keep_last: int = 2) -> None:
    """Aggressively prune old messages to reduce input tokens.

    - Removes all but the last `keep_last` screenshots.
    - Truncates DOM snapshots in old tool results.
    - Removes thinking blocks from old assistant messages.
    """
    # Find all message indices that contain screenshots
    screenshot_indices: list[tuple[int, int]] = []  # (msg_idx, content_idx)
    for msg_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item_idx, item in enumerate(content):
            d = cast(dict[str, Any], item) if isinstance(item, dict) else None
            if d is not None and d.get("type") == "image":
                screenshot_indices.append((msg_idx, item_idx))

    # Remove all but the last `keep_last` screenshots
    to_remove = (
        screenshot_indices[:-keep_last] if len(screenshot_indices) > keep_last else []
    )
    for msg_idx, item_idx in to_remove:
        messages[msg_idx]["content"][item_idx] = {
            "type": "text",
            "text": "[old screenshot removed]",
        }

    # Truncate DOM snapshots in old tool results (keep only last 2 messages with DOM)
    dom_msg_indices: list[tuple[int, int]] = []
    for msg_idx, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item_idx, item in enumerate(content):
            d = cast(dict[str, Any], item) if isinstance(item, dict) else None
            if (
                d is not None
                and d.get("type") == "text"
                and DOM_MARKER in (d.get("text") or "")
            ):
                dom_msg_indices.append((msg_idx, item_idx))

    to_trim = dom_msg_indices[:-keep_last] if len(dom_msg_indices) > keep_last else []
    for msg_idx, item_idx in to_trim:
        text = messages[msg_idx]["content"][item_idx]["text"]
        # Keep text before the DOM marker, drop the DOM itself
        cut_idx = text.index(DOM_MARKER)
        messages[msg_idx]["content"][item_idx]["text"] = (
            text[:cut_idx].rstrip() + "\n[DOM removed]"
        )

    # Truncate text in old user messages (tool results) to save tokens
    # Keep only the first line (action summary) from old tool results
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    old_users = user_indices[:-keep_last] if len(user_indices) > keep_last else []
    for msg_idx in old_users:
        content = messages[msg_idx].get("content")
        if not isinstance(content, list):
            continue
        for item_idx, item in enumerate(content):
            d = cast(dict[str, Any], item) if isinstance(item, dict) else None
            if d is not None and d.get("type") == "text":
                text = str(d.get("text", ""))
                if len(text) > 80:
                    # Keep only the first line (e.g. "Navigated to ..." or "Clicked")
                    first_line = text.split("\n", 1)[0][:80]
                    messages[msg_idx]["content"][item_idx] = {
                        "type": "text",
                        "text": first_line,
                    }

    # Strip thinking + text from older assistant messages (keep tool_use blocks only)
    assistant_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "assistant"
    ]
    old_assistants = (
        assistant_indices[:-keep_last] if len(assistant_indices) > keep_last else []
    )
    for msg_idx in old_assistants:
        content = messages[msg_idx].get("content")
        if not isinstance(content, list):
            continue
        messages[msg_idx]["content"] = [
            block
            for block in content
            if not (hasattr(block, "type") and block.type in ("thinking", "text"))
        ]


def _truncate_tool_result(tool_result: dict[str, Any], action: str) -> dict[str, Any]:
    """Truncate text content in tool results to prevent context bloat."""
    content = tool_result.get("content")
    if not content or not isinstance(content, list):
        return tool_result
    mutated = False
    truncated = []
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            if len(text) > _MAX_RESULT_CHARS:
                item = {
                    "type": "text",
                    "text": text[:_MAX_RESULT_CHARS]
                    + f"\n... [truncated, {len(text)} chars total]",
                }
                mutated = True
        truncated.append(item)
    return {**tool_result, "content": truncated} if mutated else tool_result


def _make_error_result(
    error_msg: str,
    *,
    step: int,
    run_start: float,
    bridge: ActionRouter,
    total_input_tokens: int,
    total_output_tokens: int,
) -> AgentResult:
    return AgentResult(
        success=False,
        summary="",
        action_count=step,
        action_log=bridge.action_log,
        total_duration_ms=int((time.monotonic() - run_start) * 1000),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        error=error_msg,
    )


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
) -> AgentResult:
    """Run the CUA agent loop with streaming, context management, and adaptive thinking."""
    run_start = time.monotonic()
    client = client or AsyncAnthropic()
    thinking = _AdaptiveThinking(
        base=thinking_budget, reduced=max(1024, thinking_budget // 4)
    )

    system_prompt = build_system_prompt(
        directive=directive,
        credentials=credentials,
        profile_prompt=profile_prompt,
    )
    tools = get_tools()  # Returns a deep copy with cache_control pre-applied
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

        dom = await quick_dom_snapshot(bridge.browser.page)
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
    # Stuck detection: track recent actions to detect loops
    _recent_actions: list[str] = []

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
            _prune_old_context(messages, keep_last=1)

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
                                    tool_result = _truncate_tool_result(
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
                        tr = _truncate_tool_result(tr, action)
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
                        tool_result = _truncate_tool_result(tool_result, action)
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
            for tr in tool_results:
                content = tr.get("content", [])
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        sig = item.get("text", "")[:80]
                        _recent_actions.append(sig)
            if len(_recent_actions) > MAX_RECENT:
                _recent_actions[:] = _recent_actions[-MAX_RECENT:]
            # If 4+ of the last 6 actions have the same signature, inject hint
            # Appended to the last tool_result's text to avoid mixed content types
            if len(_recent_actions) >= 4 and tool_results:
                from collections import Counter

                counts = Counter(_recent_actions[-MAX_RECENT:])
                top_count = counts.most_common(1)[0][1]
                if top_count >= 4:
                    log.warning("Stuck detected: %d repeated actions", top_count)
                    stuck_hint = (
                        "\n\n[System] You appear stuck repeating the same action. "
                        "Try a different approach: use a different selector, "
                        "navigate to a different URL, or re-read the DOM."
                    )
                    # Append to the last tool_result's text content
                    last_tr = tool_results[-1]
                    last_content = last_tr.get("content", [])
                    appended = False
                    for item in reversed(last_content):
                        if isinstance(item, dict) and item.get("type") == "text":
                            item["text"] = item["text"] + stuck_hint
                            appended = True
                            break
                    if not appended:
                        last_content.append({"type": "text", "text": stuck_hint})

            messages.append({"role": "user", "content": tool_results})

        else:
            log.warning("Agent hit max_steps limit (%d)", max_steps)

    except APIError as e:
        log.error("Anthropic API error: %s", e)
        return _make_error_result(
            f"API error: {e.message}",
            step=step,
            run_start=run_start,
            bridge=bridge,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )
    except Exception as e:
        log.error("Agent loop error: %s", e, exc_info=True)
        return _make_error_result(
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
