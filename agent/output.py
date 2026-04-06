"""Structured output types and schema-driven extraction for CUA runs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel
from pydantic_ai import Agent

from actionlog.actions import ActionLog
from settings import PRIMARY_MODEL

if TYPE_CHECKING:
    from agent.result import AgentResult
    from playbooks.schema import PlaybookResult

logger = logging.getLogger(__name__)

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

_EXTRACTION_INSTRUCTIONS = (
    "Given information gathered during browser automation, "
    "extract structured data matching the requested schema. "
    "Be concise and accurate."
)


class CuaOutput(BaseModel):
    """Structured, machine-readable result of a CUA run."""

    status: Literal["completed", "failed", "timeout", "terminated"]
    summary: str = ""
    data: dict[str, Any] | None = None
    error: str | None = None
    actions: int = 0
    duration_ms: int = 0
    session_memory: str = ""


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
    model: str = PRIMARY_MODEL,
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

    try:
        from pydantic_ai import StructuredDict

        dynamic_type = StructuredDict(output_schema, name="ExtractionResult")
        extractor = Agent(
            model,
            output_type=dynamic_type,
            instructions=_EXTRACTION_INSTRUCTIONS,
            model_settings={"max_tokens": _EXTRACTION_MAX_TOKENS},
        )

        result = await extractor.run(context)
        usage = result.usage()
        ext_input_tokens = usage.input_tokens or 0
        ext_output_tokens = usage.output_tokens or 0

        data = result.output
        if not isinstance(data, dict):
            logger.warning("Extraction returned non-object: %s", type(data).__name__)
            return None, ext_input_tokens, ext_output_tokens

        return data, ext_input_tokens, ext_output_tokens

    except (ValueError, TypeError, KeyError) as e:
        logger.warning("Structured extraction parse error: %s", e)
        return None, 0, 0
    except Exception as e:
        logger.warning("Structured extraction failed: %s", e, exc_info=True)
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
        session_memory=result.session_memory,
    )


def playbook_result_to_output(result: PlaybookResult) -> CuaOutput:
    """Convert a PlaybookResult to a CuaOutput."""
    summary, data = _extract_summary_and_data(
        result.data,
        result.extracted_text or "",
    )

    return CuaOutput(
        status="completed" if result.success else "failed",
        summary=summary,
        data=data,
        error=result.error,
        actions=len(result.step_results),
        duration_ms=result.total_duration_ms,
    )
