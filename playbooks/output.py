"""Post-execution output helpers for playbook runs."""

from __future__ import annotations

import logging
from typing import Any

from playbooks.schema import StepResult

log = logging.getLogger(__name__)


async def extract_structured_data(
    step_results: list[StepResult],
    *,
    playbook_name: str,
    output_schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Run structured extraction from successful extracted texts."""
    if not output_schema:
        return None

    extracted_texts = [
        sr.extracted_text for sr in step_results if sr.extracted_text and sr.success
    ]
    if not extracted_texts:
        return None

    try:
        from agent.output import extract_structured_output

        data, input_tokens, output_tokens = await extract_structured_output(
            summary=f"Playbook '{playbook_name}' completed successfully.",
            extracted_texts=extracted_texts,
            output_schema=output_schema,
        )
        log.info(
            "Structured extraction for playbook '%s' used %d input and %d output tokens",
            playbook_name,
            input_tokens,
            output_tokens,
        )
        return data
    except Exception as exc:
        log.warning(
            "Structured extraction failed for playbook '%s': %s",
            playbook_name,
            exc,
            exc_info=True,
        )
        return None
