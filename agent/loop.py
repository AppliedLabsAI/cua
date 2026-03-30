"""Main agent loop orchestration using Pydantic AI."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from credentials import SecretValue

from pydantic_ai import ModelSettings
from pydantic_ai.usage import UsageLimits

from actionlog.actions import ActionLog
from agent.cua_agent import cua_agent
from agent.deps import AgentDeps
from agent.output import (
    DEFAULT_OUTPUT_SCHEMA,
    collect_extracted_texts,
    extract_structured_output,
)
from agent.result import AgentResult
from bridge import DOM_MARKER
from bridge.execution import quick_dom_snapshot, quick_page_map
from bridge.router import ActionRouter
from settings import PRIMARY_MODEL

log = logging.getLogger(__name__)


async def run_agent(
    directive: str,
    bridge: ActionRouter,
    model: str = PRIMARY_MODEL,
    max_steps: int = 50,
    thinking: bool | Literal["minimal", "low", "medium", "high", "xhigh"] = "high",
    credentials: dict[str, SecretValue] | None = None,
    on_action: Callable[[ActionLog], None] | None = None,
    profile_prompt: str | None = None,
    allowed_actions: frozenset[str] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> AgentResult:
    """Run the CUA agent loop using Pydantic AI."""
    run_start = time.monotonic()

    deps = AgentDeps(
        bridge=bridge,
        directive=directive,
        max_steps=max_steps,
        credentials=credentials,
        profile_prompt=profile_prompt,
        output_schema=output_schema,
        on_action=on_action,
        allowed_actions=allowed_actions,
    )

    # Build initial user message with DOM context if available
    page_url = bridge.browser.page.url
    if page_url and page_url != "about:blank":
        page_map = await quick_page_map(
            bridge.browser.page,
            filter_config=getattr(bridge, "_filter_config", None),
        )
        if page_map:
            initial_msg = f"Current page:\n{DOM_MARKER}\n{page_map}\n\n{directive}"
        elif dom := await quick_dom_snapshot(
            bridge.browser.page,
            filter_config=getattr(bridge, "_filter_config", None),
        ):
            initial_msg = f"Current page:\n{DOM_MARKER}\n{dom}\n\n{directive}"
        else:
            initial_msg = directive
    else:
        initial_msg = directive

    try:
        result = await cua_agent.run(
            initial_msg,
            deps=deps,
            model=model,
            model_settings=ModelSettings(thinking=thinking),
            usage_limits=UsageLimits(request_limit=max_steps),
        )

        usage = result.usage()
        deps.total_input_tokens = usage.input_tokens or 0
        deps.total_output_tokens = usage.output_tokens or 0

        summary = result.output or ""

    except Exception as e:
        log.error("Agent error: %s", e, exc_info=True)
        return AgentResult(
            success=False,
            summary="",
            action_count=deps.step,
            action_log=bridge.action_log,
            total_duration_ms=int((time.monotonic() - run_start) * 1000),
            total_input_tokens=deps.total_input_tokens,
            total_output_tokens=deps.total_output_tokens,
            error=str(e),
            extracted_texts=collect_extracted_texts(bridge.action_log),
        )

    # Post-loop structured extraction
    extracted_texts = collect_extracted_texts(bridge.action_log)
    structured_data = None
    schema = output_schema or DEFAULT_OUTPUT_SCHEMA
    if summary or extracted_texts:
        structured_data, ext_in, ext_out = await extract_structured_output(
            summary=summary,
            extracted_texts=extracted_texts,
            output_schema=schema,
            model=model,
        )
        deps.total_input_tokens += ext_in
        deps.total_output_tokens += ext_out

    log.info(
        "Stats: %d actions, %dms, %d input tokens, %d output tokens",
        deps.step,
        int((time.monotonic() - run_start) * 1000),
        deps.total_input_tokens,
        deps.total_output_tokens,
    )

    return AgentResult(
        success=True,
        summary=summary,
        action_count=deps.step,
        action_log=bridge.action_log,
        total_duration_ms=int((time.monotonic() - run_start) * 1000),
        total_input_tokens=deps.total_input_tokens,
        total_output_tokens=deps.total_output_tokens,
        data=structured_data,
        extracted_texts=extracted_texts,
    )
