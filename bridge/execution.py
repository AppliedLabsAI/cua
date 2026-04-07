"""Browser action execution and page-observation orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from patchright.async_api import Page

from bridge import DOM_MARKER, ActionResult
from bridge.browser import DOM_MAX_CHARS, EXTRACT_DOM_MAX_CHARS
from bridge.observation import (
    attach_page_context,
    get_dom_snapshot,
    page_screenshot,
    quick_dom_snapshot,
    start_mutation_observer,
    stop_mutation_observer,
)
from bridge.page_actions import PageActionConfig, execute_page_action
from settings import ACTION_TIMEOUT_MS, NAVIGATION_TIMEOUT_MS, PAGE_SETTLE_TIMEOUT_MS
from telemetry.logging import C_DIM, C_RESET, fmt_status, fmt_timing

if TYPE_CHECKING:
    from bridge.browser import BrowserManager
    from credentials import SecretValue

logger = logging.getLogger(__name__)
_seq_log = logging.getLogger("bridge.sequence")

_TOOL_ACTION_CONFIG = PageActionConfig(
    action_timeout_ms=ACTION_TIMEOUT_MS,
    navigation_timeout_ms=NAVIGATION_TIMEOUT_MS,
    scroll_unit=200,
    type_delay_ms=0,
    page_settle_timeout_ms=PAGE_SETTLE_TIMEOUT_MS,
)


@dataclass(frozen=True)
class ExecutionPolicy:
    """Per-call output policy for DOM action execution."""

    include_page_context: bool = True
    dom_only: bool = False


@dataclass(frozen=True)
class ExecutionContext:
    """Runtime state passed to individual action handlers."""

    browser: BrowserManager
    filter_config: dict | None
    policy: ExecutionPolicy
    credentials: dict[str, SecretValue] | None = None

    @property
    def page(self) -> Page:
        return self.browser.page


async def _append_page_context(
    text: str,
    ctx: ExecutionContext,
    *,
    force: bool = False,
    max_dom_chars: int | None = None,
) -> str:
    if not force and not ctx.policy.include_page_context:
        return text
    return text + await attach_page_context(
        ctx.browser, ctx.filter_config, max_dom_chars=max_dom_chars
    )


def _context_for(
    browser: BrowserManager,
    params: dict[str, Any],
    *,
    include_page_context: bool,
    filter_config: dict | None,
    credentials: dict[str, SecretValue] | None,
) -> ExecutionContext:
    return ExecutionContext(
        browser=browser,
        filter_config=filter_config,
        credentials=credentials,
        policy=ExecutionPolicy(
            include_page_context=include_page_context,
            dom_only=bool(params.get("dom_only", False)),
        ),
    )


def _normalize_action(action: str) -> str:
    if action in {"type", "text"}:
        return "key_press"
    return action


async def _handle_screenshot(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    b64, dom = await asyncio.gather(
        page_screenshot(ctx.page),
        quick_dom_snapshot(ctx.page, filter_config=ctx.filter_config),
    )
    text = f"{DOM_MARKER}\n{dom}" if dom else None
    return ActionResult(screenshot_b64=b64, text=text)


async def _handle_goto(params: dict[str, Any], ctx: ExecutionContext) -> ActionResult:
    page = ctx.page
    url = params["url"]
    outcome = await execute_page_action(
        page,
        "goto",
        params,
        config=_TOOL_ACTION_CONFIG,
    )
    status = outcome.navigation_status or "unknown"
    text = f"Navigated to {url} (status {status})"
    return ActionResult(text=await _append_page_context(text, ctx))


async def _handle_click(params: dict[str, Any], ctx: ExecutionContext) -> ActionResult:
    page = ctx.page
    await start_mutation_observer(page)
    try:
        await execute_page_action(
            page,
            "click",
            params,
            config=_TOOL_ACTION_CONFIG,
        )
    except Exception:
        await stop_mutation_observer(page)
        raise

    if ctx.policy.include_page_context:
        ctx.browser.start_prefetch(
            quick_dom_snapshot(ctx.browser.page, filter_config=ctx.filter_config)
        )
    delta = await stop_mutation_observer(page)
    text = f"Clicked {delta}" if delta else "Clicked"
    return ActionResult(text=await _append_page_context(text, ctx))


async def _handle_extract(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    page = ctx.page
    outcome = await execute_page_action(
        page,
        "extract",
        params,
        config=_TOOL_ACTION_CONFIG,
    )
    text = outcome.text or ""
    # Extract results already carry the page content; attach a smaller DOM
    # (just interactive elements) so the LLM can navigate if needed.
    return ActionResult(
        text=await _append_page_context(text, ctx, max_dom_chars=EXTRACT_DOM_MAX_CHARS)
    )


async def _handle_evaluate(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    page = ctx.page
    outcome = await execute_page_action(
        page,
        "evaluate",
        params,
        config=_TOOL_ACTION_CONFIG,
    )
    text = "Evaluated script"
    if outcome.page_changed:
        dom = await quick_dom_snapshot(
            ctx.browser.page, filter_config=ctx.filter_config
        )
        if dom:
            text += f"\n\n{DOM_MARKER}\n{dom}"
    return ActionResult(text=text)


async def _handle_select(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    page = ctx.page
    outcome = await execute_page_action(
        page,
        "select",
        params,
        config=_TOOL_ACTION_CONFIG,
    )
    return ActionResult(text=outcome.text or "Selected option")


async def _handle_key_press(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    page = ctx.page
    outcome = await execute_page_action(
        page,
        "key_press",
        params,
        config=_TOOL_ACTION_CONFIG,
        credentials=ctx.credentials,
    )
    return ActionResult(text=outcome.text or "Done")


async def _handle_scroll(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    page = ctx.page
    await execute_page_action(
        page,
        "scroll",
        params,
        config=_TOOL_ACTION_CONFIG,
    )
    direction = params.get("direction", "down")
    text = f"Scrolled {direction}"
    if ctx.policy.dom_only:
        return ActionResult(text=text)
    return ActionResult(text=await _append_page_context(text, ctx))


async def _handle_get_dom(
    params: dict[str, Any],
    ctx: ExecutionContext,
) -> ActionResult:
    page = ctx.page
    dom = await get_dom_snapshot(
        page,
        selector=params.get("selector"),
        max_chars=DOM_MAX_CHARS,
        filter_config=ctx.filter_config,
    )
    if dom is None:
        return ActionResult(error="DOM snapshot unavailable")
    return ActionResult(text=dom)


_ACTION_HANDLERS = {
    "click": _handle_click,
    "evaluate": _handle_evaluate,
    "extract": _handle_extract,
    "get_dom": _handle_get_dom,
    "goto": _handle_goto,
    "key_press": _handle_key_press,
    "screenshot": _handle_screenshot,
    "scroll": _handle_scroll,
    "select": _handle_select,
}


@dataclass(frozen=True)
class SequenceExecutor:
    """Execute `execute_sequence` calls with isolated step helpers."""

    browser: BrowserManager
    filter_config: dict | None = None
    credentials: dict[str, SecretValue] | None = None

    async def run(self, params: dict) -> ActionResult:
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
        final_result = ActionResult(text="")

        for index, raw_step in enumerate(steps):
            step = self._validate_step(raw_step, index=index, results=results)
            if isinstance(step, ActionResult):
                return step

            action = step["action"]
            is_last = index == len(steps) - 1
            step_start = time.monotonic()

            with tracer.start_as_current_span(
                "cua.sequence.step",
                attributes={
                    "cua.sequence.index": index + 1,
                    "cua.sequence.total": len(steps),
                    ATTR_TOOL_ACTION: action,
                    ATTR_TOOL_SELECTOR: step.get("selector", ""),
                    ATTR_TOOL_URL: step.get("url", ""),
                },
            ) as step_span:
                result = await execute_dom_action(
                    action,
                    step,
                    self.browser,
                    include_page_context=is_last,
                    filter_config=self.filter_config,
                    credentials=self.credentials,
                )
                step_ms = int((time.monotonic() - step_start) * 1000)
                summary = summarize_action("browser_dom", action, step)

                if result.error:
                    step_span.set_attributes(
                        {
                            ATTR_TOOL_SUCCESS: False,
                            ATTR_TOOL_ERROR: result.error[:500],
                        }
                    )
                    _seq_log.info(
                        "  %s[%d/%d]%s %s %s %s",
                        C_DIM,
                        index + 1,
                        len(steps),
                        C_RESET,
                        summary,
                        fmt_timing(step_ms),
                        fmt_status(result.error),
                    )
                    return await self._step_error_result(
                        index=index,
                        action=action,
                        error=result.error,
                        results=results,
                    )

                step_span.set_attributes({ATTR_TOOL_SUCCESS: True})
                _seq_log.info(
                    "  %s[%d/%d]%s %s %s %s",
                    C_DIM,
                    index + 1,
                    len(steps),
                    C_RESET,
                    summary,
                    fmt_timing(step_ms),
                    fmt_status(None),
                )

            final_result = result
            results.append(
                f"Step {index + 1} ({action}) [{step_ms}ms]: {result.text or 'OK'}"
            )

        combined_text = "\n".join(results)
        if DOM_MARKER not in (final_result.text or ""):
            combined_text += await attach_page_context(self.browser, self.filter_config)
        return ActionResult(
            screenshot_b64=final_result.screenshot_b64,
            text=combined_text,
        )

    def _validate_step(
        self,
        raw_step: Any,
        *,
        index: int,
        results: list[str],
    ) -> dict[str, Any] | ActionResult:
        if not isinstance(raw_step, dict):
            return ActionResult(
                error=f"Step {index + 1}: invalid step format (expected object)",
                text="\n".join(results) if results else None,
            )

        step = cast(dict[str, Any], raw_step)
        action = step.get("action")
        if not isinstance(action, str) or not action.strip():
            return ActionResult(
                error=f"Step {index + 1}: missing 'action'",
                text="\n".join(results) if results else None,
            )
        if action == "execute_sequence":
            return ActionResult(
                error=f"Step {index + 1}: nested execute_sequence not allowed",
                text="\n".join(results) if results else None,
            )
        return step

    async def _step_error_result(
        self,
        *,
        index: int,
        action: str,
        error: str,
        results: list[str],
    ) -> ActionResult:
        try:
            b64 = await page_screenshot(self.browser.page)
        except Exception:
            b64 = None
        return ActionResult(
            screenshot_b64=b64,
            text="\n".join(results) if results else None,
            error=f"Step {index + 1} ({action}): {error}",
        )


async def execute_dom_action(
    action: str,
    params: dict,
    browser: BrowserManager,
    *,
    include_page_context: bool = True,
    filter_config: dict | None = None,
    credentials: dict[str, SecretValue] | None = None,
) -> ActionResult:
    """Execute a browser_dom tool action via Patchright."""
    normalized_action = _normalize_action(action)
    ctx = _context_for(
        browser,
        params,
        include_page_context=include_page_context,
        filter_config=filter_config,
        credentials=credentials,
    )

    try:
        if normalized_action == "execute_sequence":
            return await SequenceExecutor(
                browser=browser,
                filter_config=filter_config,
                credentials=credentials,
            ).run(params)

        handler = _ACTION_HANDLERS.get(normalized_action)
        if handler is None:
            return ActionResult(
                error=f"Unknown browser_dom action: {normalized_action}"
            )
        return await handler(params, ctx)
    except TimeoutError:
        timeout_used = (
            _TOOL_ACTION_CONFIG.navigation_timeout_ms
            if normalized_action == "goto"
            else _TOOL_ACTION_CONFIG.action_timeout_ms
        )
        error_msg = f"{normalized_action} timed out after {timeout_used // 1000}s"
        return await _error_with_dom_context(error_msg, browser, filter_config)
    except (ValueError, KeyError) as exc:
        return ActionResult(
            error=f"browser_dom.{normalized_action} invalid input: {exc}"
        )
    except Exception as exc:
        logger.debug(
            "browser_dom.%s unexpected error", normalized_action, exc_info=True
        )
        error_msg = f"browser_dom.{normalized_action} failed: {exc}"
        return await _error_with_dom_context(error_msg, browser, filter_config)


async def _error_with_dom_context(
    error_msg: str,
    browser: BrowserManager,
    filter_config: dict | None,
) -> ActionResult:
    """Return an error result with a fresh DOM snapshot attached.

    When an action fails (timeout, bad selector, etc.), the LLM needs to see
    the current page state to choose a valid selector for its next attempt.
    Attaching the DOM here saves one round-trip (the LLM doesn't need to
    call get_dom or screenshot before retrying).
    """
    try:
        dom = await quick_dom_snapshot(browser.page, filter_config=filter_config)
        if dom:
            return ActionResult(
                error=error_msg,
                text=f"{DOM_MARKER}\n{dom}",
            )
    except Exception:
        pass  # Best-effort; don't let DOM fetch failure mask the original error.
    return ActionResult(error=error_msg)
