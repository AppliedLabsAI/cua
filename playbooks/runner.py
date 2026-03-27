"""Playbook orchestration with deterministic execution and explicit fallback policy."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING, Any

from playbooks.executor import PlaybookStepExecutor
from playbooks.params import bind_step_params, materialize_step
from playbooks.schema import Playbook, PlaybookResult, PlaybookStep, StepResult

if TYPE_CHECKING:
    from bridge.browser import BrowserManager
    from recording.manager import RecordingManager

log = logging.getLogger(__name__)

RETRY_DELAY_S = 1.0


class PlaybookRunner:
    """Coordinate deterministic step execution and LLM fallback policy."""

    def __init__(
        self,
        browser: BrowserManager,
        recording: RecordingManager | None = None,
        step_executor: PlaybookStepExecutor | None = None,
    ) -> None:
        self._browser = browser
        self._recording = recording
        self._executor = step_executor or PlaybookStepExecutor()

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

        log.info(
            "Executing playbook '%s' (%d steps, params=%s)",
            playbook.id,
            len(playbook.steps),
            sorted(runtime_params.keys()),
        )

        for index in range(len(playbook.steps)):
            step = materialize_step(playbook, index, runtime_params)
            step_start = time.monotonic()
            log.info(
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
            )
            result.step_index = index
            result.duration_ms = int((time.monotonic() - step_start) * 1000)

            if result.success:
                step_results.append(result)
                if result.extracted_text is not None:
                    final_extracted = result.extracted_text
                if step.store_as and result.extracted_text is not None:
                    runtime_params[step.store_as] = result.extracted_text
                log.info("  Step %d OK (%dms)", index + 1, result.duration_ms)
                continue

            if result.recovery_used:
                step_results.append(result)
                return PlaybookResult(
                    playbook_id=playbook.id,
                    success=result.success,
                    step_results=step_results,
                    total_duration_ms=int((time.monotonic() - start) * 1000),
                    error=None if result.success else result.error,
                    extracted_text=result.extracted_text or final_extracted,
                )

            log.error("  Step %d aborted: %s", index + 1, result.error)
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
        log.info("Playbook '%s' completed in %dms", playbook.id, total_ms)
        return PlaybookResult(
            playbook_id=playbook.id,
            success=True,
            step_results=step_results,
            total_duration_ms=total_ms,
            extracted_text=final_extracted,
        )

    async def _run_with_policy(
        self,
        playbook: Playbook,
        step: PlaybookStep,
        remaining_steps: list[PlaybookStep],
        page: Any,
    ) -> StepResult:
        """Run a step according to its declared failure mode."""
        result = await self._executor.execute_step(step, page)
        if result.success or step.on_failure == "abort":
            return result

        log.info("  Step failed, retrying after %.1fs...", RETRY_DELAY_S)
        await asyncio.sleep(RETRY_DELAY_S)
        result = await self._executor.execute_step(step, page)
        if result.success or step.on_failure == "retry":
            return result

        log.info("  Step failed twice - handing off to LLM agent")
        llm_result = await self._llm_complete_remaining(
            playbook=playbook,
            remaining_steps=remaining_steps,
            error=result.error or "",
            page=page,
        )
        llm_result.recovery_used = True
        return llm_result

    async def _llm_complete_remaining(
        self,
        playbook: Playbook,
        remaining_steps: list[PlaybookStep],
        error: str,
        page: Any,
    ) -> StepResult:
        """Hand off the remaining work to the full LLM agent."""
        try:
            from agent.loop import run_agent
            from blinders.scope import ALL_ACTIONS
            from bridge.router import ActionRouter

            step_descriptions = "\n".join(
                f"  {index + 1}. {step.description or step.action}"
                for index, step in enumerate(remaining_steps)
            )
            directive = (
                "Complete the following task on this dashboard page.\n\n"
                f"Playbook: {playbook.name}\n"
                f"Description: {playbook.description}\n\n"
                f"A previous automation step failed with: {error}\n"
                f"You are now on: {page.url}\n\n"
                f"Remaining steps to complete:\n{step_descriptions}\n\n"
                "Complete ALL remaining steps above. Be precise with selectors."
            )

            bridge = ActionRouter(
                browser=self._browser,
                guardrail_config=playbook.guardrails.to_runtime_config(),
                recording=self._recording,
            )

            result = await run_agent(
                directive=directive,
                bridge=bridge,
                max_steps=20,
                thinking_budget=2048,
                allowed_actions=ALL_ACTIONS,
            )

            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=result.success,
                description=f"LLM completed {len(remaining_steps)} remaining steps",
                error=result.error if not result.success else None,
                extracted_text=result.summary if result.summary else None,
                recovery_used=True,
            )
        except Exception as exc:
            log.warning("LLM agent handoff failed: %s", exc)
            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=False,
                description="LLM agent handoff",
                error=f"LLM handoff failed: {exc}",
                recovery_used=True,
            )

    async def _capture_failure_screenshot(self, page: Any) -> str | None:
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
