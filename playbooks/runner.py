"""Playbook orchestration with deterministic execution and explicit fallback policy."""

from __future__ import annotations

import base64
import logging
import time
from typing import TYPE_CHECKING, Any

from playbooks.executor import PlaybookStepExecutor
from playbooks.output import extract_structured_data
from playbooks.params import bind_step_params, materialize_step
from playbooks.recovery import RETRY_DELAY_S, StepRecoveryPolicy
from playbooks.schema import Playbook, PlaybookResult, PlaybookStep, StepResult

if TYPE_CHECKING:
    from patchright.async_api import Page

    from bridge.browser import BrowserManager
    from recording.manager import RecordingManager

logger = logging.getLogger(__name__)


class PlaybookRunner:
    """Coordinate deterministic step execution and LLM fallback policy."""

    def __init__(
        self,
        browser: BrowserManager,
        recording: RecordingManager | None = None,
        step_executor: PlaybookStepExecutor | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self._browser = browser
        self._recording = recording
        self._executor = step_executor or PlaybookStepExecutor()
        self._output_schema = output_schema
        self._recovery = self._create_recovery_policy()

    async def execute(
        self,
        playbook: Playbook,
        params: dict[str, Any] | None = None,
    ) -> PlaybookResult:
        """Run all steps in a playbook, verifying each one."""
        runtime_params = dict(params or {})
        start = time.monotonic()
        step_results: list[StepResult] = []
        page = self._browser.page
        final_extracted: str | None = None

        logger.info(
            "Executing playbook '%s' (%d steps, params=%s)",
            playbook.id,
            len(playbook.steps),
            sorted(runtime_params.keys()),
        )

        for index in range(len(playbook.steps)):
            step = materialize_step(playbook, index, runtime_params)
            step_start = time.monotonic()
            logger.info(
                "  Step %d/%d: %s - %s",
                index + 1,
                len(playbook.steps),
                step.action,
                step.description or "(no description)",
            )

            remaining_steps = [
                materialize_step(playbook, step_index, runtime_params)
                for step_index in range(index, len(playbook.steps))
            ]
            result = await self._run_with_policy(
                playbook=playbook,
                step=step,
                remaining_steps=remaining_steps,
                page=page,
                runtime_params=runtime_params,
            )
            result.step_index = index
            result.duration_ms = int((time.monotonic() - step_start) * 1000)

            if result.success:
                step_results.append(result)
                if result.extracted_text is not None:
                    final_extracted = result.extracted_text
                if step.store_as and result.extracted_text is not None:
                    runtime_params[step.store_as] = result.extracted_text
                logger.info("  Step %d OK (%dms)", index + 1, result.duration_ms)
                if result.recovery_used:
                    # LLM completed all remaining steps — stop the loop
                    return PlaybookResult(
                        playbook_id=playbook.id,
                        success=True,
                        step_results=step_results,
                        total_duration_ms=int((time.monotonic() - start) * 1000),
                        extracted_text=result.extracted_text or final_extracted,
                    )
                continue

            if result.recovery_used:
                # LLM tried but failed — return failure
                step_results.append(result)
                return PlaybookResult(
                    playbook_id=playbook.id,
                    success=False,
                    step_results=step_results,
                    total_duration_ms=int((time.monotonic() - start) * 1000),
                    error=result.error,
                    extracted_text=result.extracted_text or final_extracted,
                )

            logger.error("  Step %d aborted: %s", index + 1, result.error)
            screenshot_b64 = await self._capture_failure_screenshot(page)
            return PlaybookResult(
                playbook_id=playbook.id,
                success=False,
                step_results=step_results,
                total_duration_ms=int((time.monotonic() - start) * 1000),
                error=f"Step {index + 1} ({step.description}): {result.error}",
                screenshot_b64=screenshot_b64,
                extracted_text=final_extracted,
            )

        total_ms = int((time.monotonic() - start) * 1000)
        logger.info("Playbook '%s' completed in %dms", playbook.id, total_ms)

        # Structured extraction from collected texts
        data = await extract_structured_data(
            step_results,
            playbook_name=playbook.name,
            output_schema=getattr(self, "_output_schema", None),
        )

        return PlaybookResult(
            playbook_id=playbook.id,
            success=True,
            step_results=step_results,
            total_duration_ms=total_ms,
            extracted_text=final_extracted,
            data=data,
        )

    async def _run_with_policy(
        self,
        playbook: Playbook,
        step: PlaybookStep,
        remaining_steps: list[PlaybookStep],
        page: Page,
        runtime_params: dict[str, Any] | None = None,
    ) -> StepResult:
        """Run a step according to its declared failure mode."""
        return await self._get_recovery_policy().run(
            playbook=playbook,
            step=step,
            remaining_steps=remaining_steps,
            page=page,
            runtime_params=runtime_params,
        )

    def _create_recovery_policy(self) -> StepRecoveryPolicy:
        return StepRecoveryPolicy(
            browser=self._browser,
            recording=self._recording,
            executor=self._executor,
            retry_delay_s=RETRY_DELAY_S,
            handoff_runner=self._llm_complete_remaining,
        )

    def _get_recovery_policy(self) -> StepRecoveryPolicy:
        recovery = getattr(self, "_recovery", None)
        if recovery is None:
            recovery = self._create_recovery_policy()
            self._recovery = recovery
        return recovery

    async def _llm_complete_remaining(
        self,
        playbook: Playbook,
        remaining_steps: list[PlaybookStep],
        error: str,
        page: Page,
        runtime_params: dict[str, Any] | None = None,
    ) -> StepResult:
        """Compatibility wrapper used by existing unit tests."""
        return await self._get_recovery_policy().complete_remaining_with_llm(
            playbook=playbook,
            remaining_steps=remaining_steps,
            error=error,
            page=page,
            runtime_params=runtime_params,
        )

    async def _capture_failure_screenshot(self, page: Page) -> str | None:
        try:
            raw = await page.screenshot(type="jpeg", quality=55)
            return base64.b64encode(raw).decode("ascii")
        except Exception:
            return None

    def _inject_params(
        self, step: PlaybookStep, params: dict[str, Any]
    ) -> PlaybookStep:
        """Compatibility wrapper used by existing unit tests."""
        return bind_step_params(step, params)
