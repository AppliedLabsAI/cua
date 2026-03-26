"""Action router.

Routes agent actions to the browser_dom executor (Patchright). After
navigation actions, checks for CAPTCHAs and waits for Patchright's
stealth to auto-resolve them. Enforces guardrails before every action.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from actionlog.actions import ActionLog, persist_action_log
from bridge import DOM_MARKER, ActionResult
from bridge.browser import (
    BrowserManager,
    execute_dom_action,
    page_screenshot,
    quick_dom_snapshot,
)
from bridge.captcha import handle_captcha_if_present
from guardrails import GuardrailConfig, GuardrailEngine, GuardrailResult

if TYPE_CHECKING:
    from blinders.filters import DOMBlinders

log = logging.getLogger(__name__)

# Only goto and click reliably change the page URL/frame.
_CAPTCHA_CHECK_ACTIONS = {"goto", "click"}


# ---------------------------------------------------------------------------
# ToolResult helpers — Anthropic's tool_result content format
# ---------------------------------------------------------------------------


def _image_block(b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
    }


def _screenshot_result(b64: str) -> dict:
    return {"content": [_image_block(b64)], "is_error": False}


def _text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": False}


def _screenshot_and_text_result(b64: str, text: str) -> dict:
    return {
        "content": [_image_block(b64), {"type": "text", "text": text}],
        "is_error": False,
    }


def _error_result(msg: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"Error: {msg}"}],
        "is_error": True,
    }


def _action_result_to_tool_result(result: ActionResult) -> dict:
    """Convert an ActionResult from an executor into Anthropic's tool_result format."""
    if result.error:
        return _error_result(result.error)
    if result.screenshot_b64 and result.text:
        return _screenshot_and_text_result(result.screenshot_b64, result.text)
    if result.screenshot_b64:
        return _screenshot_result(result.screenshot_b64)
    if result.text:
        return _text_result(result.text)
    return _text_result("Done")


class ActionRouter:
    """Routes tool calls to the appropriate executor with guardrails."""

    def __init__(
        self,
        browser: BrowserManager,
        guardrail_config: GuardrailConfig | None = None,
        blinders: DOMBlinders | None = None,
        directive: str = "",
    ) -> None:
        self.browser = browser
        self.guardrails = GuardrailEngine(guardrail_config)
        self.blinders = blinders
        self._filter_config = blinders.to_js_filter_config() if blinders else None
        self.action_log: list[ActionLog] = []
        self._step = 0

        # Use ScopeVerifier when blinders are active, else fall back to guardrails
        self._verifier = None
        if blinders:
            from blinders.verifier import ScopeVerifier

            self._verifier = ScopeVerifier(
                blinders.scope, self.guardrails, directive=directive,
                skip_llm_validation=True,
            )

    async def execute(self, tool_name: str, tool_input: dict) -> dict:
        """Route a tool call from Claude to the appropriate executor.

        Checks guardrails before execution. Returns Anthropic tool_result format.
        """
        self._step += 1
        action = tool_input.get("action", "")
        start = time.monotonic()

        # --- Guardrail checks ---
        guardrail_result = self._check_guardrails(tool_name, action, tool_input)
        guardrail_block = (
            guardrail_result.reason if guardrail_result and not guardrail_result.allowed else None
        )
        needs_confirmation = (
            guardrail_result.needs_confirmation if guardrail_result else False
        )
        if guardrail_block and needs_confirmation:
            # Confirmation flow: return challenge as non-error so agent can confirm
            result = ActionResult(
                text=(
                    f"⚠️ Destructive action detected: {guardrail_block}\n\n"
                    "If this is the action you intend to perform for the task, "
                    "retry the same click action to confirm. "
                    "Otherwise, find an alternative approach."
                )
            )
        elif guardrail_block:
            result = ActionResult(error=f"Guardrail blocked: {guardrail_block}")
        else:
            try:
                result = await self._dispatch(tool_name, action, tool_input)
            except Exception as e:
                result = ActionResult(error=f"{tool_name}.{action} failed: {e}")

        # Track consecutive errors for guardrail engine
        if result.error:
            stop = self.guardrails.record_error()
            if stop:
                result = ActionResult(error=stop.reason)
        else:
            self.guardrails.record_success()

        duration_ms = int((time.monotonic() - start) * 1000)
        tool_result = _action_result_to_tool_result(result)

        entry = ActionLog.now(
            step=self._step,
            tool=tool_name,
            action=action,
            tool_input=tool_input,
            duration_ms=duration_ms,
            success=result.error is None,
            result_text=result.text,
            has_screenshot=result.screenshot_b64 is not None,
            error=result.error,
        )
        self.action_log.append(entry)
        await persist_action_log(entry)

        log.info(
            "Step %d: %s.%s (%dms) %s",
            self._step,
            tool_name,
            action,
            duration_ms,
            "OK" if result.error is None else f"ERR: {result.error[:80]}",
        )

        return tool_result

    async def build_tool_result_from_raw(
        self,
        tool_name: str,
        action: str,
        tool_input: dict,
        result: ActionResult,
    ) -> dict:
        """Convert a pre-executed ActionResult into a tool_result with logging.

        Used by the parallel execution path — DOM operations run in parallel,
        then this method logs each result sequentially (safe for shared state).
        """
        self._step += 1
        if result.error:
            self.guardrails.record_error()
        else:
            self.guardrails.record_success()

        tool_result = _action_result_to_tool_result(result)

        entry = ActionLog.now(
            step=self._step,
            tool=tool_name,
            action=action,
            tool_input=tool_input,
            duration_ms=0,  # not tracked for parallel execution
            success=result.error is None,
            result_text=result.text,
            has_screenshot=result.screenshot_b64 is not None,
            error=result.error,
        )
        self.action_log.append(entry)
        await persist_action_log(entry)

        log.info(
            "Step %d: %s.%s (parallel) %s",
            self._step,
            tool_name,
            action,
            "OK" if result.error is None else f"ERR: {result.error[:80]}",
        )

        return tool_result

    def _check_guardrails(
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
            reason = self._verifier.check(
                action, tool_input,
                page_url=page_url,
                page_title=page_title,
            )
            if reason:
                return GuardrailResult(allowed=False, reason=reason)
            return None

        # Fallback: legacy guardrail checks when no blinders configured
        action_check = self.guardrails.check_action(action, tool_input)
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
                step_check = self.guardrails.check_action(step_action, step)
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
            result = await execute_dom_action(
                action,
                tool_input,
                self.browser,
                filter_config=self._filter_config,
            )
            if action in _CAPTCHA_CHECK_ACTIONS:
                result = await self._handle_captcha(result)
                # Check final URL after potential redirects — scope + guardrails
                final_url = self.browser.page.url
                if self._verifier:
                    scope_block = self._verifier._check_domain(final_url)
                    if scope_block:
                        return ActionResult(error=f"Guardrail blocked: {scope_block}")
                url_check = self.guardrails.check_url(final_url)
                if not url_check.allowed:
                    return ActionResult(error=f"Guardrail blocked: {url_check.reason}")
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
            return result

        else:
            return ActionResult(error=f"Unknown tool: {tool_name}")

    async def get_initial_screenshot(self) -> dict:
        """Capture and return the current screen state with DOM as a tool_result."""
        b64 = await page_screenshot(self.browser.page)
        dom = await quick_dom_snapshot(self.browser.page)
        if dom:
            return _screenshot_and_text_result(b64, f"{DOM_MARKER}\n{dom}")
        return _screenshot_result(b64)

    # -----------------------------------------------------------------------
    # CAPTCHA handling
    # -----------------------------------------------------------------------

    async def _handle_captcha(self, result: ActionResult) -> ActionResult:
        """Check for CAPTCHAs after navigation actions and wait for auto-resolution."""
        try:
            captcha_result = await handle_captcha_if_present(self.browser.page)
            if captcha_result.detected and captcha_result.message:
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
