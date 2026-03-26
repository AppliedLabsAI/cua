"""Conversation context management for the agent loop.

Pure functions that prune old messages to keep input tokens flat regardless
of run length. No side effects — safe to unit test.
"""

from __future__ import annotations

from typing import Any, cast

from bridge import DOM_MARKER

# Tool result text longer than this is truncated before sending back to Claude.
MAX_RESULT_CHARS = 2000


def prune_old_context(messages: list[dict[str, Any]], keep_last: int = 2) -> None:
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


def truncate_tool_result(tool_result: dict[str, Any], action: str) -> dict[str, Any]:
    """Truncate text content in tool results to prevent context bloat."""
    content = tool_result.get("content")
    if not content or not isinstance(content, list):
        return tool_result
    mutated = False
    truncated = []
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            if len(text) > MAX_RESULT_CHARS:
                item = {
                    "type": "text",
                    "text": text[:MAX_RESULT_CHARS]
                    + f"\n... [truncated, {len(text)} chars total]",
                }
                mutated = True
        truncated.append(item)
    return {**tool_result, "content": truncated} if mutated else tool_result
