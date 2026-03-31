"""Conversation context pruning for the CUA agent loop.

Registered as a Pydantic AI HistoryProcessor on the agent. Runs
automatically before each model request to keep input tokens flat
regardless of run length.

Strategies:
  - Remove old screenshots (keep last MAX_SCREENSHOTS)
  - Truncate DOM snapshots in old tool results
  - Strip thinking blocks from old assistant responses
  - Cap long text in old tool results
"""

from __future__ import annotations

from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    ToolReturnPart,
)

from bridge import DOM_MARKER

# How many recent messages (request+response pairs) to leave untouched
KEEP_LAST = 4
# Total screenshots to retain across the full history
MAX_SCREENSHOTS = 2
# Max chars for text content in old tool results (after DOM removal)
MAX_OLD_TEXT = 1000


def prune_context(messages: list[ModelMessage]) -> list[ModelMessage]:
    """HistoryProcessor: trim old messages to control token growth.

    Mutates message parts in place for efficiency (Pydantic AI passes
    ownership of the list to the processor).
    """
    if len(messages) <= KEEP_LAST:
        return messages

    old = messages[:-KEEP_LAST]
    # Recent messages are never touched
    recent = messages[-KEEP_LAST:]

    _remove_old_screenshots(old, recent)
    _truncate_old_dom(old)
    _strip_old_thinking(old)

    return old + recent


def _remove_old_screenshots(
    old: list[ModelMessage], recent: list[ModelMessage]
) -> None:
    """Keep only the last MAX_SCREENSHOTS screenshots across all messages."""
    # Collect all (message, index-in-content-list) locations of screenshots
    screenshot_locs: list[tuple[ToolReturnPart, int]] = []

    for msg in old + recent:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if isinstance(part, ToolReturnPart) and isinstance(part.content, list):
                for i, item in enumerate(part.content):
                    if isinstance(item, BinaryContent):
                        screenshot_locs.append((part, i))

    # Remove all but the last MAX_SCREENSHOTS, batched per part
    to_remove = (
        screenshot_locs[:-MAX_SCREENSHOTS]
        if len(screenshot_locs) > MAX_SCREENSHOTS
        else []
    )
    # Group indices by part to avoid repeated list conversions
    parts_to_update: dict[int, list[int]] = {}
    for part, idx in to_remove:
        pid = id(part)
        parts_to_update.setdefault(pid, []).append(idx)

    seen_parts: dict[int, ToolReturnPart] = {id(p): p for p, _ in to_remove}
    for pid, indices in parts_to_update.items():
        part = seen_parts[pid]
        if not isinstance(part.content, list):
            continue
        content = list(part.content)
        for idx in indices:
            content[idx] = "[screenshot removed]"
        part.content = content


def _truncate_old_dom(old: list[ModelMessage]) -> None:
    """Truncate DOM snapshots in old tool results, keeping action summaries."""
    for msg in old:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if not isinstance(part, ToolReturnPart):
                continue

            if isinstance(part.content, str) and DOM_MARKER in part.content:
                cut = part.content.index(DOM_MARKER)
                summary = part.content[:cut].rstrip()
                if len(summary) > MAX_OLD_TEXT:
                    summary = summary[:MAX_OLD_TEXT] + "..."
                part.content = summary + "\n[DOM removed]"

            elif isinstance(part.content, list):
                new_content = []
                for item in part.content:
                    if isinstance(item, str) and DOM_MARKER in item:
                        cut = item.index(DOM_MARKER)
                        summary = item[:cut].rstrip()
                        if len(summary) > MAX_OLD_TEXT:
                            summary = summary[:MAX_OLD_TEXT] + "..."
                        new_content.append(summary + "\n[DOM removed]")
                    elif isinstance(item, str) and len(item) > MAX_OLD_TEXT:
                        new_content.append(item[:MAX_OLD_TEXT] + "...")
                    else:
                        new_content.append(item)
                part.content = new_content


def _strip_old_thinking(old: list[ModelMessage]) -> None:
    """Remove thinking blocks from old assistant responses."""
    for msg in old:
        if not isinstance(msg, ModelResponse):
            continue
        msg.parts = [p for p in msg.parts if not isinstance(p, ThinkingPart)]
