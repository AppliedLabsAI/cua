"""Post-execution output helpers for playbook runs."""

from __future__ import annotations

from typing import Any

from playbooks.schema import StepResult


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

        data, _, _ = await extract_structured_output(
            summary=f"Playbook '{playbook_name}' completed successfully.",
            extracted_texts=extracted_texts,
            output_schema=output_schema,
        )
        return data
    except Exception:
        return None
