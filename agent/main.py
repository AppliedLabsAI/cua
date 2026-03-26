"""CLI entry point for the CUA agent — runs inside the Modal sandbox.

Reads configuration from environment variables (set by the entrypoint.sh),
initializes the bridge and browser, runs the agent loop, and reports results
to the in-sandbox status API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from opentelemetry import trace as otel_trace  # StatusCode used below

from telemetry.logging import setup_logging

setup_logging()
log = logging.getLogger("cua.agent")


async def main() -> int:
    # Import here to avoid import errors when checking syntax outside sandbox
    from agent.loop import run_agent
    from api.streaming import complete_run, init_status, push_action
    from blinders.filters import DOMBlinders
    from blinders.scope import extract_task_scope
    from bridge.browser import BrowserManager
    from bridge.router import ActionRouter
    from config import CUAConfig
    from telemetry import get_tracer, setup_telemetry
    from telemetry.metrics import active_sessions, session_duration, sessions_total
    from telemetry.propagation import extract_trace_context
    from telemetry.spans import (
        AGENT_RUN,
        AGENT_SETUP,
        ATTR_BLINDERS_ACTIONS,
        ATTR_BLINDERS_GOAL,
        ATTR_DIRECTIVE,
        ATTR_DISPLAY_HEIGHT,
        ATTR_DISPLAY_WIDTH,
        ATTR_MAX_STEPS,
        ATTR_MODEL,
        ATTR_PROFILE,
        ATTR_SESSION_ID,
        ATTR_START_URL,
        BLINDERS_EXTRACT,
        BROWSER_LAUNCH,
        EVENT_AGENT_COMPLETED,
    )

    # Initialize OTel for the inner agent process
    setup_telemetry("cua-agent")

    # Extract trace context propagated from outer API via env vars
    parent_ctx = extract_trace_context(
        os.environ.get("TRACEPARENT", ""),
        os.environ.get("TRACESTATE", ""),
    )

    # Load all configuration from environment
    try:
        config = CUAConfig.from_env()
    except (ValueError, Exception) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    from settings import get_settings

    # Use sandbox object ID as run ID (set by Modal)
    run_id = get_settings().modal_sandbox_id
    tracer = get_tracer()

    log.info(
        "Starting CUA agent: model=%s, max_steps=%d, %dx%d, profile=%s",
        config.model,
        config.max_steps,
        config.width,
        config.height,
        config.profile_name,
    )
    log.info("Directive: %s", config.directive[:200])

    # Initialize status API
    init_status(run_id)

    # Start the agent run span — linked to outer API via propagated context
    with tracer.start_as_current_span(
        AGENT_RUN,
        context=parent_ctx,
        attributes={
            ATTR_SESSION_ID: run_id,
            ATTR_DIRECTIVE: config.directive[:500],
            ATTR_MODEL: config.model,
            ATTR_MAX_STEPS: config.max_steps,
            ATTR_PROFILE: config.profile_name,
            ATTR_DISPLAY_WIDTH: config.width,
            ATTR_DISPLAY_HEIGHT: config.height,
            ATTR_START_URL: config.start_url or "",
        },
    ) as run_span:
        active_sessions().add(1)

        # Set up browser
        browser = BrowserManager()
        try:
            with tracer.start_as_current_span(AGENT_SETUP) as setup_span:
                with tracer.start_as_current_span(BROWSER_LAUNCH):
                    await browser.launch(
                        width=config.width,
                        height=config.height,
                        start_url=config.start_url,
                        proxy=config.proxy_url,
                    )
                log.info("Browser launched")

                # Set up Cognitive Blinders
                with tracer.start_as_current_span(BLINDERS_EXTRACT):
                    scope = extract_task_scope(config.directive, config.profile)
                    blinders = DOMBlinders(scope)

                setup_span.set_attributes(
                    {
                        ATTR_BLINDERS_GOAL: scope.goal_type,
                        ATTR_BLINDERS_ACTIONS: sorted(scope.allowed_actions),
                    }
                )

            log.info(
                "Blinders: goal_type=%s, allowed_actions=%d, domains=%s",
                scope.goal_type,
                len(scope.allowed_actions),
                scope.allowed_domains[:3],
            )
        except Exception as e:
            log.error("Setup failed: %s", e)
            run_span.set_status(otel_trace.StatusCode.ERROR, str(e))
            run_span.record_exception(e)
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            await complete_run(error=f"Setup failed: {e}")
            await browser.close()
            return 1

        # Run the agent loop
        try:
            bridge = ActionRouter(
                browser=browser,
                guardrail_config=config.guardrail_config,
                blinders=blinders,
                directive=config.directive,
            )
            profile_prompt = config.profile.prompt_extension if config.profile else None
            result = await run_agent(
                directive=config.directive,
                bridge=bridge,
                model=config.model,
                max_steps=config.max_steps,
                thinking_budget=config.thinking_budget,
                credentials=config.credentials,
                on_action=push_action,
                profile_prompt=profile_prompt,
                allowed_actions=scope.allowed_actions,
            )

            if result.success:
                summary_preview = (result.summary or "")[:200]
                log.info("Agent succeeded: %s", summary_preview)
                await complete_run(summary=result.summary)
                run_span.set_status(otel_trace.StatusCode.OK)
            else:
                error_text = str(result.error) if result.error is not None else ""
                log.error("Agent failed: %s", error_text)
                await complete_run(error=result.error)
                run_span.set_status(otel_trace.StatusCode.ERROR, error_text)

            run_span.add_event(
                EVENT_AGENT_COMPLETED,
                attributes={
                    "success": result.success,
                    "summary": (result.summary or "")[:200],
                    "action_count": result.action_count,
                    "total_input_tokens": result.total_input_tokens,
                    "total_output_tokens": result.total_output_tokens,
                    "total_duration_ms": result.total_duration_ms,
                },
            )

            log.info(
                "Stats: %d actions, %dms, %d input tokens, %d output tokens",
                result.action_count,
                result.total_duration_ms,
                result.total_input_tokens,
                result.total_output_tokens,
            )

            status = "success" if result.success else "failed"
            active_sessions().add(-1)
            sessions_total().add(1, {"status": status})
            session_duration().record(result.total_duration_ms, {"status": status})

            return 0 if result.success else 1

        except Exception as e:
            log.error("Agent loop crashed: %s", e, exc_info=True)
            run_span.set_status(otel_trace.StatusCode.ERROR, str(e))
            run_span.record_exception(e)
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            await complete_run(error=str(e))
            return 1

        finally:
            await browser.close()
            log.info("Browser closed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
