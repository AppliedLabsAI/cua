"""Browser action execution and page-observation helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from patchright.async_api import Page

from bridge import DOM_MARKER, ActionResult
from bridge.browser import (
    _AUTO_DOM_MAX_CHARS,
    _DOM_MAX_CHARS,
)
from bridge.js_helpers import DOM_SNAPSHOT_INIT_JS
from bridge.page_actions import PageActionConfig, execute_page_action
from settings import ACTION_TIMEOUT_MS, NAVIGATION_TIMEOUT_MS, SETTLE_TIMEOUT_MS

if TYPE_CHECKING:
    from bridge.browser import BrowserManager


# ---------------------------------------------------------------------------
# Self-healing JS calls — each checks for its helper, re-injects if missing,
# and executes in a single evaluate round-trip. This avoids separate
# "check + re-inject + call" evaluate chains that clutter Playwright traces.
# ---------------------------------------------------------------------------

_DOM_SNAPSHOT_CALL_JS = """([s, m, f, initJS]) => {
    if (!window.__domSnapshot) new Function(initJS)();
    return window.__domSnapshot ? window.__domSnapshot(s, m, f) : null;
}"""

_CAPTCHA_DETECT_CALL_JS = """(initJS) => {
    if (!window.__detectCaptcha) new Function(initJS)();
    return window.__detectCaptcha ? window.__detectCaptcha() : null;
}"""

_TOOL_ACTION_CONFIG = PageActionConfig(
    action_timeout_ms=ACTION_TIMEOUT_MS,
    navigation_timeout_ms=NAVIGATION_TIMEOUT_MS,
    scroll_unit=200,
    type_delay_ms=0,
    settle_after_click=True,
    settle_after_evaluate=True,
    settle_timeout_ms=SETTLE_TIMEOUT_MS,
    settle_sleep_s=0.0,
    smart_body_extract=True,
)


# ---------------------------------------------------------------------------
# DOM snapshot — single evaluate call
# ---------------------------------------------------------------------------


async def quick_dom_snapshot(
    page: Page,
    max_chars: int = _AUTO_DOM_MAX_CHARS,
    filter_config: dict | None = None,
) -> str:
    """Fast DOM snapshot — single evaluate in all cases."""
    try:
        raw = await page.evaluate(
            _DOM_SNAPSHOT_CALL_JS,
            [None, max_chars, filter_config, DOM_SNAPSHOT_INIT_JS],
        )
        if raw is None:
            return ""
        data = json.loads(raw)
        return f"[{data['title']}] {data['url']}\n{data['dom']}"
    except Exception:
        return ""


async def page_screenshot(page: Page) -> str:
    """Capture the browser viewport as base64 JPEG via Playwright."""
    jpeg_bytes = await page.screenshot(type="jpeg", quality=55)
    return base64.standard_b64encode(jpeg_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------


async def execute_dom_action(
    action: str,
    params: dict,
    browser: BrowserManager,
    *,
    _skip_screenshot: bool = False,
    filter_config: dict | None = None,
) -> ActionResult:
    """Execute a browser_dom tool action via Patchright."""
    if action in ("type", "text"):
        action = "key_press"

    dom_only = params.get("dom_only", False)

    try:
        page = browser.page

        if action == "screenshot":
            b64, dom = await asyncio.gather(
                page_screenshot(page),
                quick_dom_snapshot(page, filter_config=filter_config),
            )
            text = f"{DOM_MARKER}\n{dom}" if dom else None
            return ActionResult(screenshot_b64=b64, text=text)

        if action == "goto":
            url = params["url"]
            outcome = await execute_page_action(
                page,
                action,
                params,
                config=_TOOL_ACTION_CONFIG,
            )
            status = outcome.navigation_status or "unknown"
            if _skip_screenshot:
                return ActionResult(text=f"Navigated to {url} (status {status})")
            nav_text = f"Navigated to {url} (status {status})"
            dom = await quick_dom_snapshot(page, filter_config=filter_config)
            if dom:
                nav_text += f"\n\n{DOM_MARKER}\n{dom}"
            return ActionResult(text=nav_text)

        if action == "click":
            await execute_page_action(
                page,
                action,
                params,
                config=_TOOL_ACTION_CONFIG,
            )
            if _skip_screenshot:
                return ActionResult(text="Clicked")
            click_text = "Clicked"
            dom = await quick_dom_snapshot(page, filter_config=filter_config)
            if dom:
                click_text += f"\n\n{DOM_MARKER}\n{dom}"
            return ActionResult(text=click_text)

        if action in {
            "key_press",
            "scroll",
            "wait_for",
            "extract",
            "select",
            "evaluate",
        }:
            outcome = await execute_page_action(
                page,
                action,
                params,
                config=_TOOL_ACTION_CONFIG,
            )

            if action == "extract":
                return ActionResult(text=outcome.text)

            if action in {"wait_for", "key_press"}:
                return ActionResult(text=outcome.text or "Done")

            if action == "select":
                return ActionResult(text=outcome.text or "Selected option")

            if action == "evaluate":
                eval_text = "Evaluated script"
                if outcome.page_changed:
                    dom = await quick_dom_snapshot(page, filter_config=filter_config)
                    if dom:
                        eval_text += f"\n\n{DOM_MARKER}\n{dom}"
                return ActionResult(text=eval_text)

            direction = params.get("direction", "down")
            if _skip_screenshot:
                return ActionResult(text=f"Scrolled {direction}")
            scroll_text = f"Scrolled {direction}"
            if dom_only:
                return ActionResult(text=scroll_text)
            b64 = await page_screenshot(page)
            return ActionResult(screenshot_b64=b64, text=scroll_text)

        if action == "get_dom":
            selector = params.get("selector")
            raw = await page.evaluate(
                _DOM_SNAPSHOT_CALL_JS,
                [selector, _DOM_MAX_CHARS, filter_config, DOM_SNAPSHOT_INIT_JS],
            )
            if raw is None:
                return ActionResult(error="DOM snapshot unavailable")
            data = json.loads(raw)
            header = f"[{data['title']}] {data['url']}\n"
            return ActionResult(text=header + data["dom"])

        if action == "execute_sequence":
            return await _execute_sequence(params, browser, filter_config=filter_config)

        return ActionResult(error=f"Unknown browser_dom action: {action}")

    except TimeoutError:
        timeout_used = (
            _TOOL_ACTION_CONFIG.navigation_timeout_ms
            if action == "goto"
            else _TOOL_ACTION_CONFIG.action_timeout_ms
        )
        return ActionResult(error=f"{action} timed out after {timeout_used // 1000}s")
    except Exception as exc:
        return ActionResult(error=f"browser_dom.{action} failed: {exc}")


_seq_log = logging.getLogger("bridge.sequence")


async def _execute_sequence(
    params: dict,
    browser: BrowserManager,
    filter_config: dict | None = None,
) -> ActionResult:
    """Execute a sequence of browser actions in one tool call."""
    from actionlog.actions import summarize_action
    from telemetry import get_tracer
    from telemetry.spans import (
        ATTR_TOOL_ACTION,
        ATTR_TOOL_ERROR,
        ATTR_TOOL_SELECTOR,
        ATTR_TOOL_SUCCESS,
        ATTR_TOOL_URL,
    )

    steps = params.get("steps")
    if not steps or not isinstance(steps, list):
        return ActionResult(error="execute_sequence requires a 'steps' array")

    tracer = get_tracer()
    results: list[str] = []
    last_step = len(steps) - 1
    step_timings: list[int] = []
    final_result = ActionResult(text="")

    for i, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            return ActionResult(
                error=f"Step {i + 1}: invalid step format (expected object)",
                text="\n".join(results) if results else None,
            )

        step = cast(dict[str, Any], raw_step)
        action = step.get("action")
        if not isinstance(action, str) or not action.strip():
            return ActionResult(
                error=f"Step {i + 1}: missing 'action'",
                text="\n".join(results) if results else None,
            )
        if action == "execute_sequence":
            return ActionResult(
                error=f"Step {i + 1}: nested execute_sequence not allowed",
                text="\n".join(results) if results else None,
            )

        is_last = i == last_step
        step_start = time.monotonic()

        with tracer.start_as_current_span(
            "cua.sequence.step",
            attributes={
                "cua.sequence.index": i + 1,
                "cua.sequence.total": len(steps),
                ATTR_TOOL_ACTION: action,
                ATTR_TOOL_SELECTOR: step.get("selector", ""),
                ATTR_TOOL_URL: step.get("url", ""),
            },
        ) as step_span:
            result = await execute_dom_action(
                action,
                step,
                browser,
                _skip_screenshot=not is_last,
                filter_config=filter_config,
            )
            step_ms = int((time.monotonic() - step_start) * 1000)
            step_timings.append(step_ms)
            summary = summarize_action("browser_dom", action, step)

            if result.error:
                step_span.set_attributes(
                    {
                        ATTR_TOOL_SUCCESS: False,
                        ATTR_TOOL_ERROR: result.error[:500],
                    }
                )
                _seq_log.info(
                    "  [%d/%d] %s (%dms) ERR: %s",
                    i + 1,
                    len(steps),
                    summary,
                    step_ms,
                    result.error[:80],
                )
                try:
                    b64 = await page_screenshot(browser.page)
                except Exception:
                    b64 = None
                return ActionResult(
                    screenshot_b64=b64,
                    text="\n".join(results) if results else None,
                    error=f"Step {i + 1} ({action}): {result.error}",
                )

            step_span.set_attributes({ATTR_TOOL_SUCCESS: True})
            _seq_log.info(
                "  [%d/%d] %s (%dms) OK",
                i + 1,
                len(steps),
                summary,
                step_ms,
            )

        final_result = result
        results.append(f"Step {i + 1} ({action}) [{step_ms}ms]: {result.text or 'OK'}")

    combined_text = "\n".join(results)
    if DOM_MARKER not in (final_result.text or ""):
        dom = await quick_dom_snapshot(browser.page, filter_config=filter_config)
        if dom:
            combined_text += f"\n\n{DOM_MARKER}\n{dom}"
    return ActionResult(
        screenshot_b64=final_result.screenshot_b64,
        text=combined_text,
    )
