"""Action log model and persistence.

Every agent action is recorded as an ActionLog entry for debugging,
observability, and the SSE action stream.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

from pydantic import BaseModel

from api.models import ActionEvent

_LOG_DIR = "/tmp/cua-actions"
os.makedirs(_LOG_DIR, exist_ok=True)
os.chmod(_LOG_DIR, 0o700)

# Fields in tool_input that may contain large content — truncated before logging
_LARGE_FIELDS = {"text"}
_MAX_FIELD_LEN = 500


class ActionLog(BaseModel):
    """Record of a single agent action execution."""

    step: int
    timestamp: str  # ISO 8601
    tool: str  # "browser_dom"
    action: str  # e.g. "left_click", "goto", "execute"
    input_summary: str  # human-readable one-liner
    tool_input: dict  # sanitized tool input (large fields truncated)
    duration_ms: int
    success: bool
    result_text: str | None = None
    has_screenshot: bool = False
    error: str | None = None
    thinking: str | None = None

    @classmethod
    def now(
        cls,
        step: int,
        tool: str,
        action: str,
        tool_input: dict,
        duration_ms: int,
        success: bool,
        result_text: str | None = None,
        has_screenshot: bool = False,
        error: str | None = None,
        thinking: str | None = None,
    ) -> ActionLog:
        """Construct an ActionLog with the current timestamp and sanitized input."""
        return cls(
            step=step,
            timestamp=datetime.now(UTC).isoformat(),
            tool=tool,
            action=action,
            input_summary=summarize_action(tool, action, tool_input),
            tool_input=_sanitize_tool_input(tool_input),
            duration_ms=duration_ms,
            success=success,
            result_text=result_text,
            has_screenshot=has_screenshot,
            error=error,
            thinking=thinking,
        )

    def to_event(self) -> ActionEvent:
        """Convert the log entry to the API event shape."""
        return ActionEvent(
            step=self.step,
            timestamp=self.timestamp,
            tool=self.tool,
            action=self.action,
            input_summary=self.input_summary,
            duration_ms=self.duration_ms,
            success=self.success,
            result_text=self.result_text,
            has_screenshot=self.has_screenshot,
            error=self.error,
        )


def summarize_action(tool: str, action: str, params: dict) -> str:
    """Produce a human-readable one-liner for an action.

    Examples:
        "click '#submit-btn'"
        "type 'user@test.com'"
        "navigate to https://example.com"
        "execute 4-step sequence"
    """
    if tool == "browser_dom":
        selector = params.get("selector", "")
        selector_short = selector[:50] + "..." if len(selector) > 50 else selector
        match action:
            case "screenshot":
                return "take screenshot"
            case "goto":
                url = params.get("url", "?")
                return f"navigate to {url[:80]}"
            case "click":
                return f"click '{selector_short}'"
            case "key_press":
                text = params.get("text", "")
                credential_ref = params.get("credential_ref", "")
                key = params.get("key", "")
                if credential_ref and key:
                    return f"type credential '{credential_ref}' + press {key}"
                if credential_ref:
                    return f"type credential '{credential_ref}'"
                if text and key:
                    preview = text[:20] + "..." if len(text) > 20 else text
                    return f"type '{preview}' + press {key}"
                if text:
                    preview = text[:30] + "..." if len(text) > 30 else text
                    return f"type '{preview}'"
                return f"press {key}"
            case "scroll":
                direction = params.get("direction", "down")
                amount = params.get("amount", 3)
                return f"scroll {direction} {amount}x"
            case "extract":
                return f"extract text from '{selector_short}'"
            case "get_dom":
                if selector:
                    return f"get DOM (scoped: {selector_short})"
                return "get DOM"
            case "wait_for":
                state = params.get("state", "visible")
                return f"wait for '{selector_short}' to be {state}"
            case "execute_sequence":
                steps = params.get("steps", [])
                return f"execute {len(steps)}-step sequence"
            case _:
                return f"{action} '{selector_short}'"

    return f"{tool}.{action}"


def _sanitize_tool_input(tool_input: dict) -> dict:
    """Truncate large string fields recursively to avoid unbounded log growth."""
    return _sanitize_value(tool_input)


def _sanitize_value(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if (
                key in _LARGE_FIELDS
                and isinstance(item, str)
                and len(item) > _MAX_FIELD_LEN
            ):
                result[key] = item[:_MAX_FIELD_LEN] + f"... [{len(item)} chars total]"
            else:
                result[key] = _sanitize_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _sanitize_filename(s: str, max_len: int = 30) -> str:
    """Make a string safe for use in a filename."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    return safe[:max_len]


async def persist_action_log(log_entry: ActionLog) -> str:
    """Write an ActionLog entry to disk without blocking the event loop."""
    action_safe = _sanitize_filename(log_entry.action)
    filename = f"{log_entry.step:04d}_{log_entry.tool}_{action_safe}.json"
    path = os.path.join(_LOG_DIR, filename)

    def _write() -> str:
        with open(path, "w") as f:
            json.dump(log_entry.model_dump(), f, indent=2)
        return path

    return await asyncio.to_thread(_write)


async def save_action_log(action_log: list[ActionLog], path: str) -> None:
    """Save the full action log as a JSON array without blocking the event loop."""
    payload = [entry.model_dump() for entry in action_log]

    def _write() -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

    await asyncio.to_thread(_write)


# Fields excluded from SSE events — too large for real-time streaming
SSE_EXCLUDED_FIELDS = {"tool_input", "thinking"}


def format_sse_event(action: ActionLog) -> str:
    """Format an ActionLog as an SSE data line with event ID.

    Excludes tool_input and thinking (too large for streaming).
    Uses action.step as the SSE event ID for Last-Event-ID reconnection.
    Returns "id: N\\ndata: {json}\\n\\n".
    """
    payload = action.to_event().model_dump()
    return f"id: {action.step}\ndata: {json.dumps(payload)}\n\n"
