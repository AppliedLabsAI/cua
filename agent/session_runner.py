"""Sandbox session lifecycle for a configured CUA run."""

from __future__ import annotations

import logging

from anthropic import APIError
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace

from agent.loop import run_agent
from api.streaming import complete_run, push_action
from blinders.filters import DOMBlinders
from blinders.scope import extract_task_scope
from bridge.browser import BrowserManager
from bridge.router import ActionRouter
from config import CUAConfig
from recording.manager import RecordingManager
from telemetry import get_tracer
from telemetry.metrics import active_sessions, session_duration, sessions_total
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

log = logging.getLogger(__name__)


async def run_sandbox_session(
    *,
    run_id: str,
    config: CUAConfig,
    parent_ctx: otel_context.Context | None = None,
) -> int:
    """Run a single configured CUA sandbox session."""
    tracer = get_tracer()

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

                # Initialize recording
                recording: RecordingManager | None = None
                if config.recording_config.enabled:
                    recording = RecordingManager(config.recording_config, run_id)
                    await recording.start(browser.context)
                    log.info("Session recording started")

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
        except Exception as exc:
            log.error("Setup failed: %s", exc)
            run_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            run_span.record_exception(exc)
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            await complete_run(error=f"Setup failed: {exc}")
            await browser.close()
            return 1

        try:
            bridge = ActionRouter(
                browser=browser,
                guardrail_config=config.guardrail_config,
                blinders=blinders,
                directive=config.directive,
                recording=recording,
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
        except APIError as exc:
            log.error("Agent loop crashed with API error: %s", exc)
            run_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            run_span.record_exception(exc)
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            await complete_run(error=str(exc))
            return 1
        except Exception as exc:
            log.error("Agent loop crashed: %s", exc, exc_info=True)
            run_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            run_span.record_exception(exc)
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            await complete_run(error=str(exc))
            return 1
        finally:
            if recording:
                try:
                    await recording.stop()
                    if config.recording_config.upload:
                        await recording.upload(f"/recordings/{run_id}")
                    else:
                        log.info(
                            "Recordings available at %s",
                            recording.output_dir,
                        )
                except Exception as rec_exc:
                    log.warning("Recording finalization failed: %s", rec_exc)
            await browser.close()
            log.info("Browser closed")

        if result.success:
            log.info("Agent succeeded: %s", (result.summary or "")[:200])
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
