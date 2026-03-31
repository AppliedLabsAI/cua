"""Sandbox session lifecycle for a configured CUA run."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import modal
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace

from agent.loop import run_agent
from agent.output import collect_extracted_texts
from api.errors import ApiError, ApiErrorCode, classify_runtime_error, make_api_error
from api.models import RunStatusValue
from api.streaming import complete_run, persist_status, push_action
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

logger = logging.getLogger(__name__)

_RECORDING_VOLUME_NAME = "cua-recordings"


async def _commit_volume() -> None:
    """Commit the recordings volume so the outer API can read persisted data."""
    try:
        vol = modal.Volume.from_name(_RECORDING_VOLUME_NAME)
        await vol.commit.aio()
        logger.info("Committed recordings volume")
    except Exception:
        logger.warning("Failed to commit recordings volume", exc_info=True)


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

    async def _persist_run_state(
        *,
        summary: str | None = None,
        error: ApiError | str | None = None,
        data: dict[str, Any] | None = None,
        extracted_texts: list[str] | None = None,
        status: RunStatusValue | None = None,
    ) -> None:
        try:
            await complete_run(
                summary=summary,
                error=error,
                data=data,
                extracted_texts=extracted_texts,
                status=status,
            )
            await persist_status(f"/recordings/{run_id}")
            await _commit_volume()
        except Exception:
            logger.warning("Failed to persist run state", exc_info=True)

    async def _cleanup_resources() -> None:
        if recording:
            try:
                await recording.stop()
                if config.recording_config.upload:
                    await recording.upload(f"/recordings/{run_id}")
                else:
                    logger.info("Recordings available at %s", recording.output_dir)
            except Exception as rec_exc:
                logger.warning(
                    "Recording finalization failed: %s", rec_exc, exc_info=True
                )
        try:
            await browser.close()
            logger.info("Browser closed")
        except Exception:
            logger.warning("Browser close failed during cleanup", exc_info=True)

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
                    scope = await extract_task_scope(config.directive, config.profile)
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
            run_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            run_span.record_exception(exc)
            await _persist_run_state(
                error=make_api_error(
                    ApiErrorCode.SETUP_FAILED,
                    f"Setup failed: {exc}",
                    details={"run_id": run_id},
                ),
                status=RunStatusValue.FAILED,
            )
            await _cleanup_resources()
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
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
                thinking=config.thinking,
                credentials=config.credentials,
                on_action=push_action,
                profile_prompt=profile_prompt,
                allowed_actions=scope.allowed_actions,
                output_schema=config.output_schema,
            )
        except asyncio.CancelledError:
            message = "Run terminated before completion"
            if shutdown_event and shutdown_event.is_set():
                message = "Run terminated by shutdown signal"
            logger.warning(message)
            await _persist_run_state(
                error=make_api_error(
                    ApiErrorCode.RUN_TERMINATED,
                    message,
                    details={"run_id": run_id},
                ),
                extracted_texts=(
                    collect_extracted_texts(bridge.action_log) if bridge else []
                ),
                status=RunStatusValue.TERMINATED,
            )
            await _cleanup_resources()
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "terminated"})
            return 1
        except Exception as exc:
            logger.error("Agent loop crashed: %s", exc, exc_info=True)
            run_span.set_status(otel_trace.StatusCode.ERROR, str(exc))
            run_span.record_exception(exc)
            await _persist_run_state(
                error=classify_runtime_error(str(exc)),
                extracted_texts=(
                    collect_extracted_texts(bridge.action_log) if bridge else []
                ),
                status=RunStatusValue.FAILED,
            )
            await _cleanup_resources()
            active_sessions().add(-1)
            sessions_total().add(1, {"status": "failed"})
            return 1

        if result.success:
            logger.info("Agent succeeded: %s", (result.summary or "")[:200])
            await _persist_run_state(
                summary=result.summary,
                data=result.data,
                extracted_texts=result.extracted_texts,
            )
            run_span.set_status(otel_trace.StatusCode.OK)
        else:
            runtime_error = classify_runtime_error(result.error)
            if runtime_error is not None:
                error_text = runtime_error.message
                logger.error("Agent failed: %s", error_text)
                await _persist_run_state(
                    error=runtime_error,
                    extracted_texts=result.extracted_texts,
                    status=RunStatusValue.FAILED,
                )
            else:
                error_text = str(result.error) if result.error else "Unknown error"
                logger.error("Agent failed: %s", error_text)
                await _persist_run_state(
                    error=error_text,
                    extracted_texts=result.extracted_texts,
                    status=RunStatusValue.FAILED,
                )
            run_span.set_status(otel_trace.StatusCode.ERROR, error_text)

        await _cleanup_resources()

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

        logger.info(
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
