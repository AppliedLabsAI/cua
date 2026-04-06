"""Retry and LLM handoff policy for playbook execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from playbooks.schema import Playbook, PlaybookStep, StepResult

if TYPE_CHECKING:
    from patchright.async_api import Page

    from bridge.browser import BrowserManager
    from playbooks.executor import PlaybookStepExecutor
    from recording.manager import RecordingManager

logger = logging.getLogger(__name__)

RETRY_DELAY_S = 1.0


def build_handoff_directive(
    *,
    playbook: Playbook,
    remaining_steps: list[PlaybookStep],
    error: str,
    page_url: str,
    runtime_params: dict[str, Any] | None = None,
) -> str:
    """Build the directive used for LLM playbook recovery."""
    step_lines: list[str] = []
    for index, step in enumerate(remaining_steps):
        parts = [
            f"  {index + 1}. [{step.action}] {step.description or '(no description)'}"
        ]
        if step.selector:
            parts.append(f"     selector: {step.selector.primary}")
        if step.params:
            parts.append(f"     params: {step.params}")
        step_lines.append("\n".join(parts))

    param_section = ""
    if runtime_params:
        param_section = (
            "\nRuntime parameters:\n"
            + "\n".join(f"  {k} = {v}" for k, v in runtime_params.items())
            + "\n"
        )

    remaining_steps_text = "\n".join(step_lines)
    return (
        "Complete the following task on this dashboard page.\n\n"
        f"Playbook: {playbook.name}\n"
        f"Description: {playbook.description}\n"
        f"{param_section}\n"
        f"A previous automation step failed with: {error}\n"
        f"You are now on: {page_url}\n\n"
        f"Remaining steps to complete:\n{remaining_steps_text}\n\n"
        "Complete ALL remaining steps above. Be precise with selectors."
    )


class StepRecoveryPolicy:
    """Own retry and handoff behavior for failed playbook steps."""

    def __init__(
        self,
        *,
        browser: BrowserManager,
        recording: RecordingManager | None,
        executor: PlaybookStepExecutor,
        retry_delay_s: float = RETRY_DELAY_S,
        handoff_runner: Callable[..., Awaitable[StepResult]] | None = None,
    ) -> None:
        self._browser = browser
        self._recording = recording
        self._executor = executor
        self._retry_delay_s = retry_delay_s
        self._handoff_runner = handoff_runner or self.complete_remaining_with_llm

    async def run(
        self,
        *,
        playbook: Playbook,
        step: PlaybookStep,
        remaining_steps: list[PlaybookStep],
        page: Page,
        runtime_params: dict[str, Any] | None = None,
    ) -> StepResult:
        """Run a step according to its declared failure mode."""
        result = await self._executor.execute_step(
            step,
            await self._resolve_page(page),
        )
        if result.success or step.on_failure == "abort":
            return result

        logger.info("  Step failed, retrying after %.1fs...", self._retry_delay_s)
        await asyncio.sleep(self._retry_delay_s)
        result = await self._executor.execute_step(
            step,
            await self._resolve_page(page),
        )
        if result.success or step.on_failure == "retry":
            return result

        logger.info("  Step failed twice - handing off to LLM agent")
        llm_result = await self._handoff_runner(
            playbook=playbook,
            remaining_steps=remaining_steps,
            error=result.error or "",
            page=await self._resolve_page(page),
            runtime_params=runtime_params,
        )
        llm_result.recovery_used = True
        return llm_result

    async def _resolve_page(self, page: Page) -> Page:
        """Return the current active browser page when available."""
        wait_for_active_page = getattr(self._browser, "wait_for_active_page", None)
        if callable(wait_for_active_page):
            with contextlib.suppress(Exception):
                await wait_for_active_page()

        with contextlib.suppress(Exception):
            return self._browser.page
        return page

    async def complete_remaining_with_llm(
        self,
        *,
        playbook: Playbook,
        remaining_steps: list[PlaybookStep],
        error: str,
        page: Page,
        runtime_params: dict[str, Any] | None = None,
    ) -> StepResult:
        """Hand off the remaining work to the full LLM agent."""
        try:
            from agent.loop import run_agent
            from bridge.router import ActionRouter

            directive = build_handoff_directive(
                playbook=playbook,
                remaining_steps=remaining_steps,
                error=error,
                page_url=page.url,
                runtime_params=runtime_params,
            )
            from agent.memory import SessionMemory

            session_memory = SessionMemory()
            bridge = ActionRouter(
                browser=self._browser,
                guardrail_config=playbook.guardrails.to_runtime_config(),
                session_memory=session_memory,
            )
            result = await run_agent(
                directive=directive,
                bridge=bridge,
                max_steps=20,
                thinking="medium",
                session_memory=session_memory,
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
            logger.warning("LLM agent handoff failed: %s", exc)
            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=False,
                description="LLM agent handoff",
                error=f"LLM handoff failed: {exc}",
                recovery_used=True,
            )
