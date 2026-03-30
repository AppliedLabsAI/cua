"""Pydantic AI tool definitions for the CUA agent.

Defines the browser_dom tool as a typed async function following Pydantic AI
best practices. Register with an Agent via `tools=[browser_dom]`.

Also exports action constants and get_action_enum() for tests & blinders.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Literal, get_args

from pydantic_ai import BinaryContent, RunContext, ToolReturn

from agent.deps import AgentDeps

log = logging.getLogger(__name__)

BrowserAction = Literal[
    "goto",
    "click",
    "screenshot",
    "key_press",
    "scroll",
    "extract",
    "get_dom",
    "wait_for",
    "execute_sequence",
]

# Derived from BrowserAction — single source of truth.
_ALL_ACTIONS: list[str] = list(get_args(BrowserAction))

# Nested steps support all actions except execute_sequence (no recursion).
_NESTED_ACTIONS: list[str] = [a for a in _ALL_ACTIONS if a != "execute_sequence"]

# Parameter names that map directly to tool_input keys.
_TOOL_PARAMS = (
    "action",
    "selector",
    "text",
    "key",
    "url",
    "direction",
    "amount",
    "steps",
    "mode",
    "state",
    "dom_only",
)


async def browser_dom(
    ctx: RunContext[AgentDeps],
    action: BrowserAction,
    selector: str | None = None,
    text: str | None = None,
    key: str | None = None,
    url: str | None = None,
    direction: Literal["up", "down", "left", "right"] | None = None,
    amount: int | None = None,
    steps: list[dict[str, Any]] | None = None,
    mode: Literal["text", "html", "value", "markdown"] | None = None,
    state: Literal["visible", "hidden", "attached", "detached"] | None = None,
    dom_only: bool | None = None,
) -> ToolReturn:
    """Browser automation via CSS/text=/role= selectors.

    goto/click return DOM of interactive elements. screenshot returns
    screenshot + DOM. Use execute_sequence to batch multiple actions.

    Args:
        action: The browser action to perform.
        selector: CSS, text=, or role= selector for the target element.
        text: Text to type (key_press action).
        key: Key to press, e.g. Enter, Tab (key_press action).
        url: URL to navigate to (goto action).
        direction: Scroll direction (scroll action).
        amount: Scroll amount in units (scroll action).
        steps: Array of batched actions (execute_sequence action).
        mode: Content extraction mode (extract action).
        state: Element state to wait for (wait_for action).
        dom_only: Skip screenshot, return DOM only. Saves tokens.
    """
    deps = ctx.deps

    if deps.step >= deps.max_steps:
        return ToolReturn(
            return_value=(
                "[System] Max steps reached. Summarize what you accomplished "
                "and any remaining steps needed."
            ),
            metadata={"max_steps_reached": True},
        )

    local_vars = locals()
    tool_input = {k: local_vars[k] for k in _TOOL_PARAMS if local_vars[k] is not None}

    log.info("Step %d: browser_dom.%s", deps.step + 1, action)
    tool_result = await deps.bridge.execute("browser_dom", tool_input)
    deps.step += 1

    if deps.on_action and deps.bridge.action_log:
        deps.on_action(deps.bridge.action_log[-1])

    raw_content = tool_result.get("content", [])
    text_parts: list[str] = []
    rich_content: list[str | BinaryContent] = []
    for item in raw_content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                source = item.get("source", {})
                if source.get("type") == "base64":
                    rich_content.append(
                        BinaryContent(
                            data=base64.b64decode(source["data"]),
                            media_type=source.get("media_type", "image/jpeg"),
                        )
                    )

    result_text = "\n".join(text_parts) if text_parts else "Done"
    is_error = tool_result.get("is_error", False)

    if is_error:
        result_text = f"Error: {result_text}"

    return ToolReturn(
        return_value=result_text,
        content=rich_content if rich_content else None,
        metadata={"action": action, "step": deps.step, "is_error": is_error},
    )


def get_action_enum(
    allowed_actions: frozenset[str] | None = None,
) -> list[str]:
    """Return the list of browser_dom actions, optionally filtered.

    When allowed_actions is provided (from Cognitive Blinders TaskScope),
    the returned list is restricted to only those actions, sorted.
    """
    if allowed_actions is not None:
        return sorted(allowed_actions)
    return list(_ALL_ACTIONS)
