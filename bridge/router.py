"""Action router.

Routes agent actions to the browser_dom executor (Patchright). After
navigation actions, checks for CAPTCHAs and waits for Patchright's
stealth to auto-resolve them. Enforces guardrails before every action.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from actionlog.actions import ActionLog, persist_action_log, summarize_action
from bridge import DOM_MARKER, ActionResult
from bridge.background import BackgroundTasks
from bridge.browser import BrowserManager
from bridge.captcha import handle_captcha_if_present
from bridge.execution import execute_dom_action
from bridge.tool_result import action_result_to_tool_result, error_result
from bridge.url_utils import extract_visited_urls
from guardrails import GuardrailConfig, GuardrailEngine, GuardrailResult
from guardrails.stuck import StuckSeverity
from telemetry import get_tracer
from telemetry.logging import C_CYAN_BOLD, C_RESET, fmt_status, fmt_timing
from telemetry.metrics import guardrail_blocks_total, stuck_detections_total
from telemetry.spans import (
    ATTR_BROWSER_ACTION,
    ATTR_BROWSER_DOM_CHARS,
    ATTR_BROWSER_PAGE_CHANGED,
    ATTR_BROWSER_PAGE_URL,
    ATTR_GUARD_ALLOWED,
    ATTR_GUARD_CHECK_TYPE,
    ATTR_GUARD_NEEDS_CONFIRM,
    ATTR_GUARD_REASON,
    ATTR_STUCK_SEVERITY,
    ATTR_STUCK_SUMMARY,
    BROWSER_ACTION,
    CAPTCHA_HANDLE,
    EVENT_CAPTCHA,
    EVENT_STUCK,
    GUARDRAIL_CHECK,
)

if TYPE_CHECKING:
    from agent.memory import SessionMemory
    from blinders.filters import DOMBlinders
    from credentials import SecretValue

logger = logging.getLogger(__name__)

# Actions that may materially change page/frame state.
_CAPTCHA_CHECK_ACTIONS = {"goto", "click"}


@dataclass(frozen=True)
class ActionRequest:
    """Normalized action request passed through the router pipeline."""

    step: int
    tool_name: str
    action: str
    tool_input: dict


class ActionRouter:
    """Routes tool calls to the appropriate executor with guardrails."""

    def __init__(
        self,
        browser: BrowserManager,
        guardrail_config: GuardrailConfig | None = None,
        blinders: DOMBlinders | None = None,
        directive: str = "",
        session_memory: SessionMemory | None = None,
    ) -> None:
        self.browser = browser
        self.guardrails = GuardrailEngine(guardrail_config)
        self.blinders = blinders
        self._filter_config = blinders.to_js_filter_config() if blinders else None
        self.action_log: list[ActionLog] = []
        self._session_memory = session_memory
        self._step = 0
        self._stopped = False
        self._tracer = get_tracer()
        self._background = BackgroundTasks()

        self._verifier = None
        self._skip_captcha = False
        if blinders:
            from blinders.verifier import ScopeVerifier

            self._verifier = ScopeVerifier(
                blinders.scope,
                self.guardrails,
                directive=directive,
            )

    async def drain_background(self) -> None:
        """Await all pending background tasks (call before shutdown)."""
        await self._background.drain()

    async def execute(
        self,
        tool_name: str,
        tool_input: dict,
        reasoning: str | None = None,
        credentials: dict[str, SecretValue] | None = None,
    ) -> dict:
        """Route a tool call from Claude to the appropriate executor.

        Checks guardrails before execution. Returns Anthropic tool_result format.
        """
        if self._stopped:
            return error_result(
                "Session stopped due to stuck loop. "
                "Summarize what you accomplished and any remaining steps."
            )

        page_url_before = self._current_page_url()
        request = self._build_request(tool_name, tool_input)
        start = time.monotonic()

        result = await self._dispatch_phase(request, credentials=credentials)
        page_url_after = self._current_page_url()
        visited_urls = (
            extract_visited_urls(
                request.action,
                request.tool_input,
                page_url_before=page_url_before,
                page_url_after=page_url_after,
            )
            if result.error is None
            else []
        )
        result = self._postprocess_phase(
            request,
            result,
            visited_urls=visited_urls,
        )

        duration_ms = int((time.monotonic() - start) * 1000)
        self._record_action(
            request,
            result,
            duration_ms,
            reasoning=reasoning,
            visited_urls=visited_urls,
        )
        return action_result_to_tool_result(result)

    def _current_page_url(self) -> str:
        """Return the current page URL, or an empty string if unavailable."""
        with contextlib.suppress(Exception):
            return self.browser.page.url
        return ""

    def _build_request(self, tool_name: str, tool_input: dict) -> ActionRequest:
        self._step += 1
        return ActionRequest(
            step=self._step,
            tool_name=tool_name,
            action=tool_input.get("action", ""),
            tool_input=tool_input,
        )

    async def check_guardrails(self, tool_name: str, tool_input: dict) -> str | None:
        """Run guardrail checks and return a blocking reason, or None if allowed.

        Called by the before_tool_execute hook. Includes OTel spans and metrics.
        """
        action = tool_input.get("action", "")
        with self._tracer.start_as_current_span(GUARDRAIL_CHECK) as guard_span:
            guardrail_result = await self._check_guardrails(
                tool_name, action, tool_input
            )
            guardrail_block = (
                guardrail_result.reason
                if guardrail_result and not guardrail_result.allowed
                else None
            )
            check_type = "scope_verifier" if self._verifier else "legacy"
            guard_attrs = {
                ATTR_GUARD_ALLOWED: guardrail_block is None,
                ATTR_GUARD_CHECK_TYPE: check_type,
                ATTR_GUARD_NEEDS_CONFIRM: False,
            }
            if guardrail_block:
                guard_attrs[ATTR_GUARD_REASON] = guardrail_block[:500]
                guardrail_blocks_total().add(1, {"check_type": check_type})
            guard_span.set_attributes(guard_attrs)
        return guardrail_block

    async def _dispatch_phase(
        self,
        request: ActionRequest,
        *,
        credentials: dict[str, SecretValue] | None = None,
    ) -> ActionResult:
        try:
            return await self._dispatch(
                request.tool_name,
                request.action,
                request.tool_input,
                credentials=credentials,
            )
        except Exception as exc:
            return ActionResult(
                error=f"{request.tool_name}.{request.action} failed: {exc}"
            )

    def _record_action(
        self,
        request: ActionRequest,
        result: ActionResult,
        duration_ms: int,
        *,
        reasoning: str | None,
        visited_urls: list[str],
    ) -> None:
        entry = ActionLog.now(
            step=request.step,
            tool=request.tool_name,
            action=request.action,
            tool_input=request.tool_input,
            duration_ms=duration_ms,
            success=result.error is None,
            result_text=result.text,
            has_screenshot=result.screenshot_b64 is not None,
            error=result.error,
            thinking=reasoning,
        )
        self.action_log.append(entry)
        self._background.schedule(persist_action_log(entry))

        # Record to session memory so the LLM retains awareness of this action.
        if self._session_memory is not None:
            self._session_memory.record(
                step=request.step,
                action=request.action,
                tool_input=request.tool_input,
                input_summary=entry.input_summary,
                result_text=result.text,
                success=result.error is None,
                visited_urls=visited_urls,
            )

        logger.info(
            "Step %d: %s%s.%s%s %s %s",
            request.step,
            C_CYAN_BOLD,
            request.tool_name,
            request.action,
            C_RESET,
            fmt_timing(duration_ms),
            fmt_status(result.error),
        )

    def _postprocess_phase(
        self,
        request: ActionRequest,
        result: ActionResult,
        *,
        visited_urls: list[str],
    ) -> ActionResult:
        if result.error:
            stop = self.guardrails.record_error()
            if stop:
                result = ActionResult(error=stop.reason)
        else:
            self.guardrails.record_success()

        input_summary = summarize_action(
            request.tool_name,
            request.action,
            request.tool_input,
        )
        verdict = self.guardrails.record_action(
            request.action,
            request.tool_input,
            input_summary,
            success=result.error is None,
            visited_urls=visited_urls,
        )
        if verdict.severity is StuckSeverity.NONE:
            return result

        logger.warning("Stuck detected (%s): %s", verdict.severity.value, input_summary)
        stuck_detections_total().add(1, {"severity": verdict.severity.value})
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span.is_recording():
            span.add_event(
                EVENT_STUCK,
                attributes={
                    ATTR_STUCK_SEVERITY: verdict.severity.value,
                    ATTR_STUCK_SUMMARY: input_summary[:200],
                },
            )
        if verdict.severity is StuckSeverity.STOP:
            self._stopped = True
            return ActionResult(error=verdict.message)
        if result.error:
            return result

        prefix = verdict.message
        text = f"{prefix}\n{result.text}" if result.text else prefix
        return ActionResult(
            screenshot_b64=result.screenshot_b64,
            text=text,
        )

    async def _check_guardrails(
        self, tool_name: str, action: str, tool_input: dict
    ) -> GuardrailResult | None:
        """Run guardrail checks. Returns GuardrailResult if blocked, None if allowed.

        When Cognitive Blinders are active, delegates to ScopeVerifier which
        combines structural scope checks with existing guardrail protections.
        """
        if tool_name != "browser_dom":
            return None

        # Use ScopeVerifier when blinders are active (structural + LLM checks)
        if self._verifier:
            page_url = ""
            page_title = ""
            try:
                page_url = self.browser.page.url
                page_title = ""  # title requires async; URL is sufficient context
            except RuntimeError:
                pass  # browser not launched yet
            reason = await self._verifier.check(
                action,
                tool_input,
                page_url=page_url,
                page_title=page_title,
            )
            if reason:
                return GuardrailResult(allowed=False, reason=reason)
            return None

        # Fallback: legacy guardrail checks when no blinders configured
        action_check = await self.guardrails.check_action(action, tool_input)
        if not action_check.allowed:
            return action_check

        if action == "goto":
            url = tool_input.get("url", "")
            check = self.guardrails.check_navigation(url)
            if not check.allowed:
                return check
        elif action == "execute_sequence":
            for step in tool_input.get("steps", []):
                step_action = step.get("action", "")
                step_check = await self.guardrails.check_action(step_action, step)
                if not step_check.allowed:
                    return step_check
                if step_action == "goto":
                    url = step.get("url", "")
                    check = self.guardrails.check_navigation(url)
                    if not check.allowed:
                        return check

        return None

    async def _dispatch(
        self,
        tool_name: str,
        action: str,
        tool_input: dict,
        *,
        credentials: dict[str, SecretValue] | None = None,
    ) -> ActionResult:
        """Route to the correct executor."""
        if tool_name != "browser_dom":
            return ActionResult(error=f"Unknown tool: {tool_name}")

        page_url_before = ""
        with contextlib.suppress(RuntimeError):
            page_url_before = self.browser.page.url

        with self._tracer.start_as_current_span(
            BROWSER_ACTION,
            attributes={
                ATTR_BROWSER_ACTION: action,
                ATTR_BROWSER_PAGE_URL: page_url_before,
            },
        ) as browser_span:
            result = await execute_dom_action(
                action,
                tool_input,
                self.browser,
                include_page_context=True,
                filter_config=self._filter_config,
                credentials=credentials,
            )
            if action in _CAPTCHA_CHECK_ACTIONS:
                result = await self._post_navigation_phase(
                    action,
                    page_url_before,
                    result,
                    browser_span=browser_span,
                )
            result = self._apply_dom_blinders(result)
            if result.text:
                browser_span.set_attribute(ATTR_BROWSER_DOM_CHARS, len(result.text))
            return result

    async def _post_navigation_phase(
        self,
        action: str,
        page_url_before: str,
        result: ActionResult,
        *,
        browser_span,
    ) -> ActionResult:
        if not self._skip_captcha:
            result = await self._handle_captcha(result)

        final_url = self.browser.page.url
        browser_span.set_attribute(
            ATTR_BROWSER_PAGE_CHANGED, final_url != page_url_before
        )
        guardrail_block = self._check_post_navigation(final_url)
        if guardrail_block:
            return ActionResult(error=f"Guardrail blocked: {guardrail_block}")
        return result

    def _check_post_navigation(self, final_url: str) -> str | None:
        if self._verifier:
            return self._verifier.check_post_navigation(final_url)
        url_check = self.guardrails.check_url(final_url)
        if not url_check.allowed:
            return url_check.reason
        return None

    def _apply_dom_blinders(self, result: ActionResult) -> ActionResult:
        """Apply Python-side blinders post-filter on DOM content."""
        if not (self.blinders and result.text and DOM_MARKER in result.text):
            return result

        marker_idx = result.text.index(DOM_MARKER)
        prefix = result.text[:marker_idx]
        dom_content = result.text[marker_idx + len(DOM_MARKER) + 1 :]
        filtered = self.blinders.filter_snapshot(dom_content)
        return ActionResult(
            screenshot_b64=result.screenshot_b64,
            text=f"{prefix}{DOM_MARKER}\n{filtered}",
            error=result.error,
        )

    # -----------------------------------------------------------------------
    # CAPTCHA handling
    # -----------------------------------------------------------------------

    async def _handle_captcha(self, result: ActionResult) -> ActionResult:
        """Check for CAPTCHAs after navigation actions and wait for auto-resolution."""
        try:
            captcha_result = await handle_captcha_if_present(self.browser.page)
            if captcha_result.detected:
                with self._tracer.start_as_current_span(CAPTCHA_HANDLE) as captcha_span:
                    captcha_span.add_event(
                        EVENT_CAPTCHA,
                        attributes={
                            "captcha_type": captcha_result.captcha_type or "",
                            "resolved": captcha_result.resolved,
                            "wait_time_ms": captcha_result.wait_time_ms,
                        },
                    )
                if captcha_result.message:
                    text = (
                        f"{captcha_result.message}\n{result.text}"
                        if result.text
                        else captcha_result.message
                    )
                    return ActionResult(
                        screenshot_b64=result.screenshot_b64,
                        text=text,
                        error=result.error,
                    )
        except Exception as e:
            logger.warning("CAPTCHA handling failed: %s", e)
        return result
