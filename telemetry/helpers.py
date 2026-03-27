"""Reusable telemetry helpers for span creation and metric recording.

Eliminates duplication of tool-execution spans, LLM span attributes,
and content-block event handling across streaming and fallback paths.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from opentelemetry import trace as otel_trace

from bridge import DOM_MARKER
from telemetry.metrics import steps_total, tool_duration
from telemetry.spans import (
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MAX_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
    ATTR_GENAI_THINKING,
    ATTR_LLM_HAS_TOOL_CALLS,
    ATTR_LLM_STREAMING,
    ATTR_LLM_TEXT_RESPONSE,
    ATTR_TOOL_ACTION,
    ATTR_TOOL_DURATION_MS,
    ATTR_TOOL_ERROR,
    ATTR_TOOL_HAS_SCREENSHOT,
    ATTR_TOOL_INPUT_SUMMARY,
    ATTR_TOOL_NAME,
    ATTR_TOOL_SELECTOR,
    ATTR_TOOL_STEP,
    ATTR_TOOL_SUCCESS,
    ATTR_TOOL_URL,
    EVENT_TEXT_OUTPUT,
    EVENT_THINKING,
    TOOL_EXECUTE,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer

    from bridge.router import ActionRouter

log = logging.getLogger(__name__)


def _strip_dom(text: str) -> str:
    """Strip DOM snapshot content from text to avoid bloating span attributes."""
    if DOM_MARKER in text:
        return text[: text.index(DOM_MARKER)].rstrip()
    return text


def llm_span_attrs(
    model: str, max_tokens: int, thinking: str | bool, streaming: bool
) -> dict[str, Any]:
    """Build initial attributes for an LLM call span."""
    return {
        ATTR_GENAI_MODEL: model,
        ATTR_GENAI_MAX_TOKENS: max_tokens,
        ATTR_GENAI_THINKING: str(thinking),
        ATTR_LLM_STREAMING: streaming,
    }


def finalize_llm_span(
    span: Span,
    input_tokens: int,
    output_tokens: int,
    has_tool_calls: bool,
    text_response: str | None = None,
) -> None:
    """Set post-call attributes on an LLM span."""
    attrs: dict[str, Any] = {
        ATTR_GENAI_INPUT_TOKENS: input_tokens,
        ATTR_GENAI_OUTPUT_TOKENS: output_tokens,
        ATTR_LLM_HAS_TOOL_CALLS: has_tool_calls,
    }
    if text_response:
        attrs[ATTR_LLM_TEXT_RESPONSE] = _strip_dom(text_response)
    span.set_attributes(attrs)


def record_text_block(block: Any, llm_span: Span, text_parts: list[str]) -> None:
    """Process a text content block: log, collect, and emit span event."""
    text = getattr(block, "text", "") or ""
    if text:
        text_parts.append(text)
        stripped = _strip_dom(text)
        log.debug("Agent text: %s", stripped)
        llm_span.add_event(EVENT_TEXT_OUTPUT, attributes={"text": stripped})


def record_thinking_block(block: Any, llm_span: Span) -> None:
    """Process a thinking content block: log and emit span event."""
    thinking_text = getattr(block, "thinking", "") or ""
    if thinking_text:
        log.debug("Thinking: %s", thinking_text)
        llm_span.add_event(EVENT_THINKING, attributes={"thinking_text": thinking_text})


async def execute_tool_with_span(
    tracer: Tracer,
    bridge: ActionRouter,
    block_name: str,
    block_id: str,
    block_input: dict[str, Any],
    step: int,
) -> dict[str, Any]:
    """Execute a tool call inside an instrumented span.

    Creates a TOOL_EXECUTE span, runs the action via bridge, sets all
    standard attributes, records metrics, and returns the tool_result dict.
    """
    action = block_input.get("action", "")
    tool_start = time.monotonic()

    with tracer.start_as_current_span(
        TOOL_EXECUTE,
        attributes={
            ATTR_TOOL_NAME: block_name,
            ATTR_TOOL_ACTION: action,
            ATTR_TOOL_STEP: step,
            ATTR_TOOL_SELECTOR: block_input.get("selector", ""),
            ATTR_TOOL_URL: block_input.get("url", ""),
        },
    ) as tool_span:
        tool_result = await bridge.execute(block_name, block_input)

        is_error = tool_result.get("is_error", False)
        tool_ms = int((time.monotonic() - tool_start) * 1000)

        attrs: dict[str, Any] = {
            ATTR_TOOL_SUCCESS: not is_error,
            ATTR_TOOL_DURATION_MS: tool_ms,
        }
        if is_error:
            tool_span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR))
            content = tool_result.get("content", [{}])
            if content:
                attrs[ATTR_TOOL_ERROR] = _strip_dom(str(content[0].get("text", "")))
        if bridge.action_log:
            entry = bridge.action_log[-1]
            attrs[ATTR_TOOL_HAS_SCREENSHOT] = entry.has_screenshot
            attrs[ATTR_TOOL_INPUT_SUMMARY] = entry.input_summary
        tool_span.set_attributes(attrs)

        tool_duration().record(tool_ms, {"action": action})
        steps_total().add(1, {"action": action, "success": not is_error})

    return {"type": "tool_result", "tool_use_id": block_id, **tool_result}
