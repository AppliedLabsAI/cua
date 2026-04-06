"""Deterministic playbook step execution against a browser page."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from bridge.page_actions import PageActionConfig, execute_page_action
from playbooks.schema import (
    PlaybookStep,
    SelectorStrategy,
    StepResult,
    StepVerification,
)
from settings import (
    ACTION_TIMEOUT_MS,
    NAVIGATION_TIMEOUT_MS,
    SELECTOR_PROBE_TIMEOUT_MS,
    SETTLE_SLEEP_S,
    SETTLE_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bridge.browser import BrowserManager


class PlaybookStepExecutor:
    """Run deterministic playbook steps without owning fallback policy."""

    def __init__(self, browser: BrowserManager | None = None) -> None:
        self._browser = browser
        self._last_extracted: str | None = None
        self._config = PageActionConfig(
            action_timeout_ms=ACTION_TIMEOUT_MS,
            navigation_timeout_ms=NAVIGATION_TIMEOUT_MS,
            scroll_unit=1,
            type_delay_ms=50,
            settle_after_click=True,
            settle_after_evaluate=True,
            settle_timeout_ms=SETTLE_TIMEOUT_MS,
            settle_sleep_s=SETTLE_SLEEP_S,
        )

    async def execute_step(self, step: PlaybookStep, page: Any) -> StepResult:
        """Execute a single playbook step and verify its outcome."""
        try:
            await self._run_action(step, page)
        except Exception as exc:
            logger.warning(
                "Playbook step action failed (%s): %s",
                step.description or step.action,
                exc,
            )
            return StepResult(
                step_index=0,
                action=step.action,
                success=False,
                description=step.description,
                error=str(exc),
            )

        if step.verify:
            try:
                verify_page = await self._verification_page(page)
                await self._verify(step.verify, verify_page)
            except Exception as exc:
                logger.warning(
                    "Playbook step verification failed (%s): %s",
                    step.description or step.action,
                    exc,
                )
                return StepResult(
                    step_index=0,
                    action=step.action,
                    success=False,
                    description=step.description,
                    error=f"Verification failed: {exc}",
                )

        extracted = self._last_extracted
        self._last_extracted = None
        return StepResult(
            step_index=0,
            action=step.action,
            success=True,
            description=step.description,
            extracted_text=extracted,
        )

    async def resolve_selector(
        self,
        strategy: SelectorStrategy | None,
        page: Any,
    ) -> str:
        """Try each selector in the fallback chain until one matches."""
        if not strategy:
            raise ValueError("Step requires a selector but none provided")

        for selector in strategy.all_selectors:
            try:
                handle = await page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=SELECTOR_PROBE_TIMEOUT_MS,
                )
                if handle:
                    logger.debug("Selector resolved: %s", selector)
                    return selector
            except Exception:
                continue

        raise RuntimeError(
            f"No selector matched: tried {strategy.all_selectors}"
            + (f" ({strategy.description})" if strategy.description else "")
        )

    async def _run_action(self, step: PlaybookStep, page: Any) -> None:
        params = dict(step.params)

        # llm_extract: extract page content, then analyze with LLM
        if step.action == "llm_extract":
            extract_params = dict(params)
            if step.selector:
                extract_params["selector"] = await self.resolve_selector(
                    step.selector, page
                )
            else:
                extract_params.setdefault("selector", "body")
            extract_params.setdefault("mode", "markdown")

            outcome = await execute_page_action(
                page, "extract", extract_params, config=self._config
            )
            page_content = outcome.text or ""

            analysis = await self._llm_analyze(
                page_content,
                prompt=step.prompt,
                directive=params.get("directive", ""),
            )
            self._last_extracted = analysis
            return

        if step.action in {"click", "select", "extract"} or (
            step.action == "key_press" and step.selector
        ):
            params["selector"] = await self.resolve_selector(step.selector, page)
        elif step.action == "wait_for":
            resolved = None
            if step.selector:
                with contextlib.suppress(RuntimeError):
                    resolved = await self.resolve_selector(step.selector, page)
            if resolved:
                params["selector"] = resolved

        outcome = await execute_page_action(
            page,
            step.action,
            params,
            config=self._config,
        )
        if step.action == "extract":
            self._last_extracted = outcome.text

    async def _verification_page(self, page: Any) -> Any:
        """Rebind verification to the latest active page after actions."""
        browser = self._browser
        if browser is None:
            return page

        wait_for_active_page = getattr(browser, "wait_for_active_page", None)
        if callable(wait_for_active_page):
            with contextlib.suppress(Exception):
                await wait_for_active_page()

        with contextlib.suppress(Exception):
            return browser.page
        return page

    async def _llm_analyze(
        self,
        page_content: str,
        *,
        prompt: str = "",
        directive: str = "",
    ) -> str:
        """Send extracted page content to LLM for analysis."""
        from pydantic_ai import Agent

        from settings import UTILITY_MODEL

        analysis_prompt = prompt or (
            "Analyze the page content and answer the user's question."
        )
        user_message = (
            f"## Directive\n{directive}\n\n"
            f"## Task\n{analysis_prompt}\n\n"
            f"## Page Content\n{page_content}"
        )

        agent: Agent[None, str] = Agent(
            UTILITY_MODEL,
            instructions=(
                "You are analyzing a web page's content to answer a specific question. "
                "Be concise and factual. Only report what you can see in the page content. "
                "If the information is not present, say so clearly."
            ),
            output_retries=3,
        )
        result = await agent.run(user_message)
        logger.info("LLM analysis complete (%d chars)", len(result.output))
        return result.output

    async def _verify(self, verification: StepVerification, page: Any) -> None:
        timeout = verification.timeout_ms

        if verification.expect_url_contains:
            deadline = time.monotonic() + (timeout / 1000)
            while time.monotonic() < deadline:
                if verification.expect_url_contains in page.url:
                    break
                await asyncio.sleep(SETTLE_SLEEP_S)
            else:
                raise AssertionError(
                    f"URL '{page.url}' does not contain '{verification.expect_url_contains}'"
                )

        if verification.expect_element_visible:
            try:
                await page.wait_for_selector(
                    verification.expect_element_visible,
                    state="visible",
                    timeout=timeout,
                )
            except Exception as exc:
                raise AssertionError(
                    f"Element not visible: {verification.expect_element_visible}"
                ) from exc

        if verification.expect_element_gone:
            try:
                await page.wait_for_selector(
                    verification.expect_element_gone,
                    state="hidden",
                    timeout=timeout,
                )
            except Exception as exc:
                raise AssertionError(
                    f"Element still present: {verification.expect_element_gone}"
                ) from exc

        if verification.expect_text_on_page:
            try:
                await page.locator(
                    f"text={verification.expect_text_on_page}"
                ).first.wait_for(state="visible", timeout=timeout)
            except Exception as exc:
                raise AssertionError(
                    f"Text not found on page: '{verification.expect_text_on_page}'"
                ) from exc
