"""Post-execution output helpers for playbook runs."""

from __future__ import annotations

import logging
from typing import Any

from playbooks.schema import StepResult

logger = logging.getLogger(__name__)


def collect_step_extracted_texts(step_results: list[StepResult]) -> list[str]:
    """Return extracted text from successful steps only."""
    return [
        sr.extracted_text for sr in step_results if sr.success and sr.extracted_text
    ]


async def extract_structured_data(
    step_results: list[StepResult],
    *,
    summary: str,
    playbook_name: str,
    output_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, int, int]:
    """Run structured extraction from successful extracted texts."""
    extracted_texts = collect_step_extracted_texts(step_results)

    try:
        from agent.output import maybe_extract_structured_output

        data, input_tokens, output_tokens = await maybe_extract_structured_output(
            summary=summary or f"Playbook '{playbook_name}' completed successfully.",
            extracted_texts=extracted_texts,
            output_schema=output_schema,
        )
        logger.info(
            "Structured extraction for playbook '%s' used %d input and %d output tokens",
            playbook_name,
            input_tokens,
            output_tokens,
        )
        return data, input_tokens, output_tokens
    except Exception as exc:
        logger.warning(
            "Structured extraction failed for playbook '%s': %s",
            playbook_name,
            exc,
            exc_info=True,
        )
        return None, 0, 0
