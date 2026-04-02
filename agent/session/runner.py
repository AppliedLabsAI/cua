"""Sandbox session lifecycle for a configured CUA run."""

from __future__ import annotations

import asyncio
import logging

from opentelemetry import context as otel_context

from agent.loop import run_agent
from agent.memory import SessionMemory
from agent.output import collect_extracted_texts
from agent.session.finalizer import RunFinalizer, RunOutcome
from api.streaming import push_action
from blinders.filters import DOMBlinders
from blinders.scope import extract_task_scope
from bridge.browser import BrowserManager
from bridge.router import ActionRouter
from config import CUAConfig
from recording.manager import RecordingManager
from telemetry import get_tracer
from telemetry.metrics import active_sessions
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
)

logger = logging.getLogger(__name__)


async def run_sandbox_session(
    *,
    run_id: str,
    config: CUAConfig,
    parent_ctx: otel_context.Context | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> int:
    """Run a single configured CUA sandbox session."""
    tracer = get_tracer()
    recording: RecordingManager | None = None
    bridge: ActionRouter | None = None
    session_memory = SessionMemory()

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
                logger.info("Browser launched")

                if config.recording_config.enabled:
                    recording = RecordingManager(config.recording_config, run_id)
                    await recording.start(browser.context)
                    logger.info("Session recording started")

                with tracer.start_as_current_span(BLINDERS_EXTRACT):
                    scope = await extract_task_scope(
                        config.directive, config.profile, config.start_url
                    )
                    blinders = DOMBlinders(scope)

                setup_span.set_attributes(
                    {
                        ATTR_BLINDERS_GOAL: scope.goal_type,
                        ATTR_BLINDERS_ACTIONS: sorted(scope.allowed_actions),
                    }
                )

            logger.info(
                "Blinders: goal_type=%s, allowed_actions=%d, domains=%s",
                scope.goal_type,
                len(scope.allowed_actions),
                scope.allowed_domains[:3],
            )
        except Exception as exc:
            logger.error("Setup failed: %s", exc)
            run_span.record_exception(exc)
            # recording may have started before the failure — use it if set
            setup_finalizer = RunFinalizer(
                run_id=run_id,
                browser=browser,
                recording=recording,
                recording_upload=config.recording_config.upload,
            )
            outcome = RunOutcome.setup_failed(run_id, exc)
            run_span.set_status(outcome.trace_status, outcome.trace_message or "")
            return await setup_finalizer.finalize(outcome)

        finalizer = RunFinalizer(
            run_id=run_id,
            browser=browser,
            recording=recording,
            recording_upload=config.recording_config.upload,
        )

        try:
            bridge = ActionRouter(
                browser=browser,
                guardrail_config=config.guardrail_config,
                blinders=blinders,
                directive=config.directive,
                session_memory=session_memory,
            )
            profile_prompt = config.profile.prompt_extension if config.profile else None
            result = await run_agent(
                directive=config.directive,
                bridge=bridge,
                model=config.model,
                max_steps=config.max_steps,
                thinking=config.thinking,
                credentials=config.credentials,
                on_action=push_action,
                profile_prompt=profile_prompt,
                allowed_actions=scope.allowed_actions,
                output_schema=config.output_schema,
                session_memory=session_memory,
            )
        except asyncio.CancelledError:
            message = "Run terminated before completion"
            if shutdown_event and shutdown_event.is_set():
                message = "Run terminated by shutdown signal"
            logger.warning(message)
            outcome = RunOutcome.terminated(
                run_id,
                message,
                extracted_texts=(
                    collect_extracted_texts(bridge.action_log) if bridge else []
                ),
                session_memory=session_memory.render(),
            )
            run_span.set_status(outcome.trace_status, outcome.trace_message or "")
            return await finalizer.finalize(outcome)
        except Exception as exc:
            logger.error("Agent loop crashed: %s", exc, exc_info=True)
            run_span.record_exception(exc)
            outcome = RunOutcome.crashed(
                str(exc),
                extracted_texts=(
                    collect_extracted_texts(bridge.action_log) if bridge else []
                ),
                session_memory=session_memory.render(),
            )
            run_span.set_status(outcome.trace_status, outcome.trace_message or "")
            return await finalizer.finalize(outcome)

        outcome = RunOutcome.from_agent_result(result)
        if result.success:
            logger.info("Agent succeeded: %s", (result.summary or "")[:200])
        else:
            logger.error("Agent failed: %s", outcome.trace_message or "Unknown error")
        run_span.set_status(outcome.trace_status, outcome.trace_message or "")
        return await finalizer.finalize(outcome, result=result)
