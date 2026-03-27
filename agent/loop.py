"""Main agent loop orchestration."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from anthropic import APIError, AsyncAnthropic

from actionlog.actions import ActionLog
from agent.context import prune_old_context
from agent.llm_runtime import (
    append_hint_to_last_result,
    fallback_llm_call,
    streaming_llm_call,
)
from agent.output import (
    DEFAULT_OUTPUT_SCHEMA,
    collect_extracted_texts,
    extract_structured_output,
)
from agent.prompts import build_system_prompt
from agent.result import AgentResult, make_error_result
from agent.stuck import StuckDetector
from agent.thinking import AdaptiveThinking
from agent.tools import get_tools
from bridge import DOM_MARKER
from bridge.execution import quick_dom_snapshot
from bridge.router import ActionRouter
from settings import AGENT_MODEL
from telemetry import get_tracer
from telemetry.metrics import (
    errors_total,
    iteration_duration,
    llm_call_duration,
    llm_calls_total,
    llm_tokens_input,
    llm_tokens_output,
)
from telemetry.spans import (
    AGENT_ITERATION,
    ATTR_ITER_NUMBER,
    ATTR_ITER_STREAMING,
    ATTR_ITER_THINKING_BUDGET,
    ATTR_ITER_TOOL_CALLS,
    EVENT_STUCK,
)

log = logging.getLogger(__name__)

_BETA_FLAGS = ["interleaved-thinking-2025-05-14"]
_MAX_TOKENS = 2048


def _record_llm_metrics(
    api_ms: int,
    model: str,
    is_streaming: bool,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record LLM call metrics in one place."""
    labels = {"model": model, "streaming": is_streaming}
    llm_calls_total().add(1, labels)
    llm_call_duration().record(api_ms, labels)
    llm_tokens_input().record(input_tokens, {"model": model})
    llm_tokens_output().record(output_tokens, {"model": model})


async def run_agent(
    directive: str,
    bridge: ActionRouter,
    model: str = AGENT_MODEL,
    max_steps: int = 50,
    thinking_budget: int = 4096,
    credentials: dict | None = None,
    on_action: Callable[[ActionLog], None] | None = None,
    client: AsyncAnthropic | None = None,
    profile_prompt: str | None = None,
    allowed_actions: frozenset[str] | None = None,
    output_schema: dict | None = None,
) -> AgentResult:
    """Run the CUA agent loop with streaming, context management, and adaptive thinking."""
    run_start = time.monotonic()
    client = client or AsyncAnthropic()
    thinking = AdaptiveThinking(
        base=thinking_budget, reduced=max(1024, thinking_budget // 4)
    )
    stuck_detector = StuckDetector()

    system_prompt = build_system_prompt(
        directive=directive,
        credentials=credentials,
        profile_prompt=profile_prompt,
    )
    tools = get_tools(allowed_actions=allowed_actions)
    system = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Skip initial screenshot — the agent's first goto/click returns one anyway,
    # so an upfront screenshot just wastes ~1500 image tokens.
    # Only include initial DOM if browser is already on a page (for start_url flows).
    page_url = bridge.browser.page.url
    if page_url and page_url != "about:blank":
        dom = await quick_dom_snapshot(
            bridge.browser.page,
            filter_config=getattr(bridge, "_filter_config", None),
        )
        if dom:
            initial_content = [
                {
                    "type": "text",
                    "text": f"Current page:\n{DOM_MARKER}\n{dom}\n\n{directive}",
                }
            ]
        else:
            initial_content = [{"type": "text", "text": directive}]
    else:
        initial_content = [{"type": "text", "text": directive}]

    messages: list[dict] = [{"role": "user", "content": initial_content}]

    total_input_tokens = 0
    total_output_tokens = 0
    text_parts: list[str] = []
    step = 0

    def _api_kwargs() -> dict:
        return {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "tools": tools,
            "messages": messages,
            "system": system,
            "betas": _BETA_FLAGS,
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking.budget,
            },
        }

    tracer = get_tracer()
    iteration_num = 0

    try:
        while step < max_steps:
            iteration_num += 1
            iter_start = time.monotonic()

            with tracer.start_as_current_span(
                AGENT_ITERATION,
                attributes={
                    ATTR_ITER_NUMBER: iteration_num,
                    ATTR_ITER_THINKING_BUDGET: thinking.budget,
                },
            ) as iter_span:
                prune_old_context(messages, keep_last=1)

                tool_results: list[dict] = []
                response_content = []
                api_call_start = time.monotonic()
                last_input_tokens = 0
                last_output_tokens = 0
                _skip_remaining = False
                is_streaming = True

                try:
                    (
                        tool_results,
                        response_content,
                        last_input_tokens,
                        last_output_tokens,
                    ) = await streaming_llm_call(
                        client=client,
                        api_kwargs=_api_kwargs,
                        tracer=tracer,
                        model=model,
                        max_tokens=_MAX_TOKENS,
                        thinking=thinking,
                        bridge=bridge,
                        step_base=step,
                        iter_span=iter_span,
                        text_parts=text_parts,
                        on_action=on_action,
                    )
                    step += len(tool_results)
                except APIError:
                    raise
                except Exception as stream_err:
                    is_streaming = False
                    log.warning("Streaming failed (%s), falling back", stream_err)
                    (
                        tool_results,
                        response_content,
                        last_input_tokens,
                        last_output_tokens,
                    ) = await fallback_llm_call(
                        client=client,
                        api_kwargs=_api_kwargs,
                        tracer=tracer,
                        model=model,
                        max_tokens=_MAX_TOKENS,
                        thinking=thinking,
                        bridge=bridge,
                        step_base=step,
                        text_parts=text_parts,
                        on_action=on_action,
                    )
                    step += len(tool_results)

                total_input_tokens += last_input_tokens
                total_output_tokens += last_output_tokens

                api_ms = int((time.monotonic() - api_call_start) * 1000)
                log.info(
                    "API call: %dms, %d tool calls, tokens: %d in",
                    api_ms,
                    len(tool_results),
                    last_input_tokens,
                )
                _record_llm_metrics(
                    api_ms, model, is_streaming, last_input_tokens, last_output_tokens
                )

                messages.append({"role": "assistant", "content": response_content})

                if not tool_results:
                    log.info("Agent finished (no tool calls)")
                    iter_span.set_attributes(
                        {
                            ATTR_ITER_TOOL_CALLS: 0,
                            ATTR_ITER_STREAMING: is_streaming,
                        }
                    )
                    break

                # --- Stuck detection ---
                stuck_detector.record(tool_results)
                hint = stuck_detector.get_hint()
                if hint and tool_results:
                    iter_span.add_event(EVENT_STUCK, attributes={"hint": hint[:200]})
                    append_hint_to_last_result(tool_results, hint)

                messages.append({"role": "user", "content": tool_results})

                iter_span.set_attributes(
                    {
                        ATTR_ITER_TOOL_CALLS: len(tool_results),
                        ATTR_ITER_STREAMING: is_streaming,
                    }
                )
                iteration_duration().record(int((time.monotonic() - iter_start) * 1000))

        else:
            log.warning("Agent hit max_steps limit (%d)", max_steps)

    except APIError as e:
        log.error("Anthropic API error: %s", e)
        errors_total().add(1, {"component": "agent", "error_type": "api_error"})
        return make_error_result(
            f"API error: {e.message}",
            step=step,
            run_start=run_start,
            bridge=bridge,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )
    except Exception as e:
        log.error("Agent loop error: %s", e, exc_info=True)
        errors_total().add(1, {"component": "agent", "error_type": "loop_error"})
        return make_error_result(
            str(e),
            step=step,
            run_start=run_start,
            bridge=bridge,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
        )

    summary = "\n".join(text_parts) if text_parts else f"Completed in {step} steps"
    extracted_texts = collect_extracted_texts(bridge.action_log)

    structured_data = None
    schema = output_schema or DEFAULT_OUTPUT_SCHEMA
    if summary or extracted_texts:
        structured_data, ext_in, ext_out = await extract_structured_output(
            summary=summary,
            extracted_texts=extracted_texts,
            output_schema=schema,
            client=client,
            model=model,
        )
        total_input_tokens += ext_in
        total_output_tokens += ext_out

    return AgentResult(
        success=True,
        summary=summary,
        action_count=step,
        action_log=bridge.action_log,
        total_duration_ms=int((time.monotonic() - run_start) * 1000),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        data=structured_data,
        extracted_texts=extracted_texts,
    )
