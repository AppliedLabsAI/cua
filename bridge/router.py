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
from guardrails import GuardrailConfig, GuardrailEngine, GuardrailResult
from guardrails.stuck import StuckSeverity
from telemetry import get_tracer
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
    from blinders.filters import DOMBlinders
    from recording.manager import RecordingManager

log = logging.getLogger(__name__)

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
        recording: RecordingManager | None = None,
    ) -> None:
        self.browser = browser
        self.guardrails = GuardrailEngine(guardrail_config)
        self.blinders = blinders
        self._filter_config = blinders.to_js_filter_config() if blinders else None
        self.action_log: list[ActionLog] = []
        self._step = 0
        self._stopped = False
        self._tracer = get_tracer()
        self._recording = recording
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

    async def execute(self, tool_name: str, tool_input: dict) -> dict:
        """Route a tool call from Claude to the appropriate executor.

        Checks guardrails before execution. Returns Anthropic tool_result format.
        """
        if self._stopped:
            return error_result(
                "Session stopped due to stuck loop. "
                "Summarize what you accomplished and any remaining steps."
            )

        request = self._build_request(tool_name, tool_input)
        start = time.monotonic()

        guardrail_block = await self._guard_phase(request)
        result = await self._dispatch_phase(request, guardrail_block)
        result = self._postprocess_phase(request, result)

        duration_ms = int((time.monotonic() - start) * 1000)
        tool_result = action_result_to_tool_result(result)

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
        )
        self.action_log.append(entry)
        self._background.schedule(persist_action_log(entry))

        if self._recording and result.screenshot_b64:
            self._background.schedule(
                self._recording.on_screenshot(
                    request.step, request.action, result.screenshot_b64
                )
            )

        log.info(
            "Step %d: %s.%s (%dms) %s",
            request.step,
            request.tool_name,
            request.action,
            duration_ms,
            "OK" if result.error is None else f"ERR: {result.error[:80]}",
        )

        return tool_result

    def _build_request(self, tool_name: str, tool_input: dict) -> ActionRequest:
        self._step += 1
        return ActionRequest(
            step=self._step,
            tool_name=tool_name,
            action=tool_input.get("action", ""),
            tool_input=tool_input,
        )

    async def _guard_phase(self, request: ActionRequest) -> str | None:
        with self._tracer.start_as_current_span(GUARDRAIL_CHECK) as guard_span:
            guardrail_result = await self._check_guardrails(
                request.tool_name, request.action, request.tool_input
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
        guardrail_block: str | None,
    ) -> ActionResult:
        if guardrail_block:
            return ActionResult(error=f"Guardrail blocked: {guardrail_block}")

        try:
            return await self._dispatch(
                request.tool_name,
                request.action,
                request.tool_input,
            )
        except Exception as exc:
            return ActionResult(
                error=f"{request.tool_name}.{request.action} failed: {exc}"
            )

    def _postprocess_phase(
        self,
        request: ActionRequest,
        result: ActionResult,
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
            input_summary, success=result.error is None
        )
        if verdict.severity is StuckSeverity.NONE:
            return result

        log.warning("Stuck detected (%s): %s", verdict.severity.value, input_summary)
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
        self, tool_name: str, action: str, tool_input: dict
    ) -> ActionResult:
        """Route to the correct executor."""
        if tool_name == "browser_dom":
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
                )

                if action in _CAPTCHA_CHECK_ACTIONS:
                    if not self._skip_captcha:
                        result = await self._handle_captcha(result)
                    final_url = self.browser.page.url
                    browser_span.set_attribute(
                        ATTR_BROWSER_PAGE_CHANGED, final_url != page_url_before
                    )
                    if self._verifier:
                        scope_block = self._verifier._check_domain(final_url)
                        if scope_block:
                            return ActionResult(
                                error=f"Guardrail blocked: {scope_block}"
                            )
                    url_check = self.guardrails.check_url(final_url)
                    if not url_check.allowed:
                        return ActionResult(
                            error=f"Guardrail blocked: {url_check.reason}"
                        )

                # Apply Python-side blinders post-filter on DOM content
                if self.blinders and result.text and DOM_MARKER in result.text:
                    marker_idx = result.text.index(DOM_MARKER)
                    prefix = result.text[:marker_idx]
                    dom_content = result.text[marker_idx + len(DOM_MARKER) + 1 :]
                    filtered = self.blinders.filter_snapshot(dom_content)
                    result = ActionResult(
                        screenshot_b64=result.screenshot_b64,
                        text=f"{prefix}{DOM_MARKER}\n{filtered}",
                        error=result.error,
                    )

                if result.text:
                    browser_span.set_attribute(ATTR_BROWSER_DOM_CHARS, len(result.text))

            return result

        else:
            return ActionResult(error=f"Unknown tool: {tool_name}")

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
            log.warning("CAPTCHA handling failed: %s", e)
        return result
