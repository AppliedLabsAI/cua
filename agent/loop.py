"""Main agent loop orchestration using Pydantic AI."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from credentials import SecretValue

from pydantic_ai import ModelSettings
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from actionlog.actions import ActionLog
from agent.circuit_breaker import CircuitBreaker, CircuitOpenError
from agent.cua_agent import cua_agent
from agent.deps import AgentDeps
from agent.output import (
    collect_extracted_texts,
    extract_structured_output,
)
from agent.result import AgentResult, make_error_result
from bridge.execution import attach_page_context
from bridge.router import ActionRouter
from settings import PRIMARY_MODEL
from telemetry.logging import C_BLUE_BOLD, C_DIM, C_RESET

logger = logging.getLogger(__name__)


def _error(
    msg: str,
    *,
    deps: AgentDeps,
    bridge: ActionRouter,
    run_start: float,
) -> AgentResult:
    """Build an AgentResult for an error exit (thin wrapper over make_error_result)."""
    return make_error_result(
        msg,
        step=deps.step,
        run_start=run_start,
        bridge=bridge,
        total_input_tokens=deps.total_input_tokens,
        total_output_tokens=deps.total_output_tokens,
    )


# Module-level circuit breaker shared across runs in the same process.
# In Modal each sandbox is a separate process (one run), but in local dev
# mode multiple sequential runs share this breaker — preventing repeated
# attempts when the LLM provider is down.
_llm_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout_s=30.0)


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
        page_ctx = await attach_page_context(
            bridge.browser, filter_config=bridge._filter_config
        )
        if page_ctx:
            initial_msg = f"Current page:{page_ctx}\n\n{directive}"
        else:
            initial_msg = directive
    else:
        initial_msg = directive

    try:
        _llm_circuit.check()

        result = await cua_agent.run(
            initial_msg,
            deps=deps,
            model=model,
            model_settings=ModelSettings(thinking=thinking),
            usage_limits=UsageLimits(request_limit=max_steps),
        )

        _llm_circuit.record_success()
        usage = result.usage()
        deps.total_input_tokens = usage.input_tokens or 0
        deps.total_output_tokens = usage.output_tokens or 0

        summary = result.output or ""

    except CircuitOpenError as e:
        logger.error("LLM circuit breaker open: %s", e)
        return _error(str(e), deps=deps, bridge=bridge, run_start=run_start)

    except UsageLimitExceeded:
        # Step limit reached — not a provider error, don't trip the breaker.
        _llm_circuit.record_success()
        logger.warning("Agent reached step limit (%d steps)", max_steps)
        return _error(
            f"Reached maximum step limit ({max_steps} steps)",
            deps=deps,
            bridge=bridge,
            run_start=run_start,
        )

    except Exception as e:
        _llm_circuit.record_failure()
        logger.error("Agent error: %s", e, exc_info=True)
        return _error(str(e), deps=deps, bridge=bridge, run_start=run_start)

    # Post-loop structured extraction (only when caller provides a schema)
    extracted_texts = collect_extracted_texts(bridge.action_log)
    structured_data = None
    if output_schema and (summary or extracted_texts):
        structured_data, ext_in, ext_out = await extract_structured_output(
            summary=summary,
            extracted_texts=extracted_texts,
            output_schema=output_schema,
            model=model,
        )
        deps.total_input_tokens += ext_in
        deps.total_output_tokens += ext_out

    total_ms = int((time.monotonic() - run_start) * 1000)
    logger.info(
        "%sStats:%s %d actions, %s%dms%s, %d input tokens, %d output tokens",
        C_BLUE_BOLD,
        C_RESET,
        deps.step,
        C_DIM,
        total_ms,
        C_RESET,
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
