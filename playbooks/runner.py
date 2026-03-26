"""PlaybookRunner — deterministic executor with verification and LLM fallback."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from patchright.async_api import Page

from playbooks.schema import (
    Playbook,
    PlaybookResult,
    PlaybookStep,
    SelectorStrategy,
    StepResult,
    StepVerification,
)

if TYPE_CHECKING:
    from bridge.browser import BrowserManager
    from recording.manager import RecordingManager

log = logging.getLogger(__name__)

_ACTION_TIMEOUT = 5000  # 5s for clicks, key presses
_NAVIGATION_TIMEOUT = 7_000  # 7s for page loads
_RETRY_DELAY_S = 1.0  # Delay before retrying a failed step


class PlaybookRunner:
    """Execute playbook steps deterministically with verification.

    For each step:
    1. Resolve selector (try primary, then fallbacks)
    2. Execute action via Playwright directly (no LLM overhead)
    3. Verify post-condition (URL, element visibility, text)
    4. On failure: retry once, then LLM recovery or abort
    """

    def __init__(
        self,
        browser: BrowserManager,
        recording: RecordingManager | None = None,
    ) -> None:
        self._browser = browser
        self._recording = recording
        self._last_extracted: str | None = None

    async def execute(
        self,
        playbook: Playbook,
        params: dict | None = None,
    ) -> PlaybookResult:
        """Run all steps in a playbook, verifying each one."""
        params = params or {}
        start = time.monotonic()
        step_results: list[StepResult] = []
        page = self._browser.page

        log.info(
            "Executing playbook '%s' (%d steps, params=%s)",
            playbook.id,
            len(playbook.steps),
            list(params.keys()),
        )

        for i, step in enumerate(playbook.steps):
            step_start = time.monotonic()
            resolved_step = self._inject_params(step, params)

            log.info(
                "  Step %d/%d: %s — %s",
                i + 1,
                len(playbook.steps),
                resolved_step.action,
                resolved_step.description or "(no description)",
            )

            result = await self._execute_step(resolved_step, page)

            if not result.success:
                # Retry once after a short delay
                log.info(
                    "  Step %d failed, retrying after %.1fs...", i + 1, _RETRY_DELAY_S
                )
                await asyncio.sleep(_RETRY_DELAY_S)
                result = await self._execute_step(resolved_step, page)

            # After 2 failures: hand off to the full LLM agent to complete
            # ALL remaining steps. The page state has likely diverged from
            # what the playbook expects.
            if not result.success and resolved_step.on_failure != "abort":
                log.info(
                    "  Step %d failed twice — handing off to LLM agent for remaining %d steps",
                    i + 1,
                    len(playbook.steps) - i,
                )
                remaining_steps = [
                    self._inject_params(s, params) for s in playbook.steps[i:]
                ]
                llm_result = await self._llm_complete_remaining(
                    playbook, remaining_steps, params, result.error or "", page
                )
                llm_result.step_index = i
                llm_result.duration_ms = int((time.monotonic() - step_start) * 1000)
                llm_result.recovery_used = True
                step_results.append(llm_result)

                return PlaybookResult(
                    playbook_id=playbook.id,
                    success=llm_result.success,
                    step_results=step_results,
                    total_duration_ms=int((time.monotonic() - start) * 1000),
                    error=None if llm_result.success else llm_result.error,
                    extracted_text=llm_result.extracted_text,
                )

            if not result.success:
                log.error("  Step %d aborted: %s", i + 1, result.error)
                screenshot_b64 = await self._capture_failure_screenshot(page)
                return PlaybookResult(
                    playbook_id=playbook.id,
                    success=False,
                    step_results=step_results,
                    total_duration_ms=int((time.monotonic() - start) * 1000),
                    error=f"Step {i + 1} ({resolved_step.description}): {result.error}",
                    screenshot_b64=screenshot_b64,
                )

            result.step_index = i
            result.duration_ms = int((time.monotonic() - step_start) * 1000)
            step_results.append(result)

            log.info("  Step %d OK (%dms)", i + 1, result.duration_ms)

        total_ms = int((time.monotonic() - start) * 1000)
        log.info("Playbook '%s' completed in %dms", playbook.id, total_ms)

        return PlaybookResult(
            playbook_id=playbook.id,
            success=True,
            step_results=step_results,
            total_duration_ms=total_ms,
        )

    # -----------------------------------------------------------------------
    # Step execution
    # -----------------------------------------------------------------------

    async def _execute_step(self, step: PlaybookStep, page: Page) -> StepResult:
        """Execute a single playbook step and verify its outcome."""
        try:
            await self._run_action(step, page)
        except Exception as exc:
            return StepResult(
                step_index=0,
                action=step.action,
                success=False,
                description=step.description,
                error=str(exc),
            )

        # Verify post-conditions
        if step.verify:
            try:
                await self._verify(step.verify, page)
            except Exception as exc:
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=False,
                    description=step.description,
                    error=f"Verification failed: {exc}",
                )

        # Capture extracted text if this was an extract step
        extracted = self._last_extracted
        self._last_extracted = None

        return StepResult(
            step_index=0,
            action=step.action,
            success=True,
            description=step.description,
            extracted_text=extracted,
        )

    async def _run_action(self, step: PlaybookStep, page: Page) -> None:
        """Execute the Playwright action for a step."""
        action = step.action

        if action == "goto":
            url = step.params.get("url", "")
            await page.goto(
                url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT
            )

        elif action == "click":
            selector = await self._resolve_selector(step.selector, page)
            await page.click(selector, timeout=_ACTION_TIMEOUT)
            # Wait for any navigation triggered by the click
            await self._wait_for_stable(page)

        elif action == "key_press":
            text = step.params.get("text")
            key = step.params.get("key")
            if text:
                # If a selector is provided, type into that element
                if step.selector:
                    selector = await self._resolve_selector(step.selector, page)
                    await page.fill(selector, text, timeout=_ACTION_TIMEOUT)
                else:
                    await page.keyboard.type(text, delay=50)
            if key:
                await page.keyboard.press(key)

        elif action == "scroll":
            direction = step.params.get("direction", "down")
            amount = step.params.get("amount", 600)
            delta_x = (
                amount
                if direction == "right"
                else (-amount if direction == "left" else 0)
            )
            delta_y = (
                amount if direction == "down" else (-amount if direction == "up" else 0)
            )
            await page.mouse.wheel(delta_x, delta_y)
            await asyncio.sleep(0.3)

        elif action == "wait_for":
            selector = step.params.get("selector", "")
            state = step.params.get("state", "visible")
            timeout = step.params.get("timeout_ms", _ACTION_TIMEOUT)
            await page.wait_for_selector(selector, state=state, timeout=timeout)

        elif action == "select":
            selector = await self._resolve_selector(step.selector, page)
            value = step.params.get("value", "")
            await page.select_option(selector, value, timeout=_ACTION_TIMEOUT)

        elif action == "evaluate":
            # Execute JavaScript on the page. The script can navigate,
            # extract values, or manipulate the DOM.
            script = step.params.get("script", "")
            await page.evaluate(script)
            await self._wait_for_stable(page)

        elif action == "extract":
            # Extract text content from an element.
            selector = await self._resolve_selector(step.selector, page)
            mode = step.params.get("mode", "text")
            if mode == "value":
                text = await page.input_value(selector, timeout=_ACTION_TIMEOUT)
            elif mode == "html":
                text = await page.inner_html(selector, timeout=_ACTION_TIMEOUT)
            else:
                text = await page.inner_text(selector, timeout=_ACTION_TIMEOUT)
            # Store extracted text so it can be reported
            self._last_extracted = text

        else:
            raise ValueError(f"Unknown playbook action: {action}")

    # -----------------------------------------------------------------------
    # Selector resolution
    # -----------------------------------------------------------------------

    async def _resolve_selector(
        self,
        strategy: SelectorStrategy | None,
        page: Page,
    ) -> str:
        """Try each selector in the fallback chain until one matches."""
        if not strategy:
            raise ValueError("Step requires a selector but none provided")

        for selector in strategy.all_selectors:
            try:
                handle = await page.wait_for_selector(
                    selector, state="visible", timeout=800
                )
                if handle:
                    log.debug("Selector resolved: %s", selector)
                    return selector
            except Exception:
                continue

        raise RuntimeError(
            f"No selector matched: tried {strategy.all_selectors}"
            + (f" ({strategy.description})" if strategy.description else "")
        )

    # -----------------------------------------------------------------------
    # Verification
    # -----------------------------------------------------------------------

    async def _verify(self, v: StepVerification, page: Page) -> None:
        """Assert post-conditions after a step. Raises on failure."""
        timeout = v.timeout_ms

        if v.expect_url_contains:
            deadline = time.monotonic() + (timeout / 1000)
            while time.monotonic() < deadline:
                if v.expect_url_contains in page.url:
                    break
                await asyncio.sleep(0.3)
            else:
                raise AssertionError(
                    f"URL '{page.url}' does not contain '{v.expect_url_contains}'"
                )

        if v.expect_element_visible:
            try:
                await page.wait_for_selector(
                    v.expect_element_visible, state="visible", timeout=timeout
                )
            except Exception as e:
                raise AssertionError(
                    f"Element not visible: {v.expect_element_visible}"
                ) from e

        if v.expect_element_gone:
            try:
                await page.wait_for_selector(
                    v.expect_element_gone, state="hidden", timeout=timeout
                )
            except Exception as e:
                raise AssertionError(
                    f"Element still present: {v.expect_element_gone}"
                ) from e

        if v.expect_text_on_page:
            try:
                await page.locator(f"text={v.expect_text_on_page}").first.wait_for(
                    state="visible", timeout=timeout
                )
            except Exception as e:
                raise AssertionError(
                    f"Text not found on page: '{v.expect_text_on_page}'"
                ) from e

    # -----------------------------------------------------------------------
    # LLM agent handoff for failed steps
    # -----------------------------------------------------------------------

    async def _llm_complete_remaining(
        self,
        playbook: Playbook,
        remaining_steps: list[PlaybookStep],
        params: dict,
        error: str,
        page: Page,
    ) -> StepResult:
        """Hand off to the full LLM agent to complete all remaining steps.

        When a playbook step fails twice, the page state has likely diverged
        from expectations. Instead of patching individual steps, give the LLM
        full context about what needs to happen and let it drive the browser
        to completion.
        """
        try:
            from agent.loop import run_agent
            from blinders.scope import ALL_ACTIONS
            from bridge.router import ActionRouter
            from guardrails import GuardrailConfig

            # Build a directive that describes what the LLM needs to accomplish
            step_descriptions = "\n".join(
                f"  {j + 1}. {s.description or s.action}"
                for j, s in enumerate(remaining_steps)
            )
            directive = (
                f"Complete the following task on this dashboard page.\n\n"
                f"Playbook: {playbook.name}\n"
                f"Description: {playbook.description}\n\n"
                f"A previous automation step failed with: {error}\n"
                f"You are now on: {page.url}\n\n"
                f"Remaining steps to complete:\n{step_descriptions}\n\n"
                f"Complete ALL remaining steps above. Be precise with selectors."
            )

            log.info("LLM agent handoff — directive:\n%s", directive)

            # Use playbook-defined guardrails, or safe defaults
            if playbook.guardrails:
                guardrail_config = GuardrailConfig.from_dict(playbook.guardrails)
            else:
                guardrail_config = GuardrailConfig()
            bridge = ActionRouter(
                browser=self._browser,
                guardrail_config=guardrail_config,
                recording=self._recording,
            )

            result = await run_agent(
                directive=directive,
                bridge=bridge,
                max_steps=20,
                thinking_budget=2048,
                allowed_actions=ALL_ACTIONS,
            )

            # Extract any text from the agent's summary
            extracted = result.summary if result.summary else None

            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=result.success,
                description=f"LLM completed {len(remaining_steps)} remaining steps",
                error=result.error if not result.success else None,
                extracted_text=extracted,
            )
        except Exception as exc:
            log.warning("LLM agent handoff failed: %s", exc)
            return StepResult(
                step_index=0,
                action="llm_handoff",
                success=False,
                description="LLM agent handoff",
                error=f"LLM handoff failed: {exc}",
            )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _wait_for_stable(self, page: Page) -> None:
        """Wait for network idle and DOM stability after navigation."""
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=3000)

    async def _capture_failure_screenshot(self, page: Page) -> str | None:
        """Capture a screenshot for debugging when a step fails."""
        try:
            raw = await page.screenshot(type="jpeg", quality=55)
            return base64.b64encode(raw).decode()
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Parameter injection
    # -----------------------------------------------------------------------

    def _inject_params(self, step: PlaybookStep, params: dict) -> PlaybookStep:
        """Replace {param_name} placeholders in step params and selectors."""
        if not params:
            return step

        def _replace(text: str) -> str:
            for key, value in params.items():
                text = text.replace(f"{{{key}}}", str(value))
            return text

        # Deep copy params with replacement
        new_params = {
            k: (_replace(v) if isinstance(v, str) else v)
            for k, v in step.params.items()
        }

        # Replace in selector
        new_selector = None
        if step.selector:
            new_selector = SelectorStrategy(
                primary=_replace(step.selector.primary),
                fallbacks=[_replace(f) for f in step.selector.fallbacks],
                description=step.selector.description,
            )

        # Replace in verification
        new_verify = None
        if step.verify:
            new_verify = StepVerification(
                expect_url_contains=(
                    _replace(step.verify.expect_url_contains)
                    if step.verify.expect_url_contains
                    else None
                ),
                expect_element_visible=(
                    _replace(step.verify.expect_element_visible)
                    if step.verify.expect_element_visible
                    else None
                ),
                expect_element_gone=(
                    _replace(step.verify.expect_element_gone)
                    if step.verify.expect_element_gone
                    else None
                ),
                expect_text_on_page=(
                    _replace(step.verify.expect_text_on_page)
                    if step.verify.expect_text_on_page
                    else None
                ),
                timeout_ms=step.verify.timeout_ms,
            )

        return PlaybookStep(
            action=step.action,
            params=new_params,
            selector=new_selector,
            verify=new_verify,
            description=_replace(step.description) if step.description else "",
            on_failure=step.on_failure,
        )
