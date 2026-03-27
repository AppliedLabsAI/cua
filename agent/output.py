"""Structured output types and schema-driven extraction for CUA runs."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from actionlog.actions import ActionLog
from settings import AGENT_MODEL

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from agent.result import AgentResult
    from playbooks.schema import PlaybookResult

log = logging.getLogger(__name__)

_EXTRACTION_MAX_TOKENS = 1024

# Default schema used when no custom output_schema is provided.
# Extracts a concise answer and any structured details the agent found.
DEFAULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "A single concise sentence summarizing the outcome of the task",
        },
        "result": {
            "type": "string",
            "description": "The direct answer or main finding from the task",
        },
        "details": {
            "type": "object",
            "description": "Any structured data points, key-value pairs, or tabular data extracted during the task. Use descriptive keys.",
        },
    },
    "required": ["summary", "result"],
}


class CuaOutput(BaseModel):
    """Structured, machine-readable result of a CUA run."""

    status: Literal["completed", "failed", "timeout", "terminated"]
    summary: str = ""
    data: dict[str, Any] | None = None
    error: str | None = None
    actions: int = 0
    duration_ms: int = 0


def collect_extracted_texts(action_log: list[ActionLog]) -> list[str]:
    """Gather all text from extract actions in the action log."""
    return [
        entry.result_text
        for entry in action_log
        if entry.action == "extract" and entry.result_text and entry.success
    ]


async def extract_structured_output(
    summary: str,
    extracted_texts: list[str],
    output_schema: dict[str, Any],
    client: AsyncAnthropic,
    model: str = AGENT_MODEL,
) -> tuple[dict[str, Any] | None, int, int]:
    """Post-loop LLM call to extract structured data matching output_schema.

    Returns (data_dict, input_tokens, output_tokens). Returns (None, 0, 0) on failure.
    """
    context_parts: list[str] = []
    if summary:
        context_parts.append(f"## Agent Summary\n{summary}")
    if extracted_texts:
        joined = "\n---\n".join(extracted_texts)
        context_parts.append(f"## Extracted Data\n{joined}")

    if not context_parts:
        return None, 0, 0

    context = "\n\n".join(context_parts)
    schema_str = json.dumps(output_schema, separators=(",", ":"))

    prompt = (
        "Given the following information gathered during browser automation:\n\n"
        f"{context}\n\n"
        "Extract structured data matching this JSON Schema. "
        f"Respond ONLY with valid JSON, no markdown fences or explanation.\n\n"
        f"Schema:\n{schema_str}"
    )

    ext_input_tokens = 0
    ext_output_tokens = 0

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=_EXTRACTION_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

        ext_input_tokens = response.usage.input_tokens or 0
        ext_output_tokens = response.usage.output_tokens or 0

        text = ""
        for block in response.content:
            if block.type == "text":
                text += getattr(block, "text", "")

        text = text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        data = json.loads(text)
        if not isinstance(data, dict):
            log.warning("Extraction returned non-object JSON: %s", type(data).__name__)
            return None, ext_input_tokens, ext_output_tokens

        return data, ext_input_tokens, ext_output_tokens

    except json.JSONDecodeError as e:
        log.warning("Extraction produced invalid JSON: %s", e)
        return None, ext_input_tokens, ext_output_tokens
    except Exception as e:
        log.warning("Structured extraction failed: %s", e)
        return None, 0, 0


def _extract_summary_and_data(
    data: dict[str, Any] | None,
    fallback_summary: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Extract 'summary' from data dict (if present) without mutating the original."""
    if data and isinstance(data, dict):
        data = dict(data)
        summary = data.pop("summary", "")
    else:
        summary = ""
    return summary or fallback_summary, data


def agent_result_to_output(result: AgentResult) -> CuaOutput:
    """Convert an AgentResult to a CuaOutput."""
    summary, data = _extract_summary_and_data(result.data, result.summary)

    return CuaOutput(
        status="completed" if result.success else "failed",
        summary=summary,
        data=data,
        error=result.error,
        actions=result.action_count,
        duration_ms=result.total_duration_ms,
    )


def playbook_result_to_output(result: PlaybookResult) -> CuaOutput:
    """Convert a PlaybookResult to a CuaOutput."""
    summary, data = _extract_summary_and_data(result.data)

    return CuaOutput(
        status="completed" if result.success else "failed",
        summary=summary,
        data=data,
        error=result.error,
        actions=len(result.step_results),
        duration_ms=result.total_duration_ms,
    )
