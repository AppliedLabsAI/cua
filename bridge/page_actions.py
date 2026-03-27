"""Shared primitive page actions used by both AI tools and playbook execution."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from bridge.js_helpers import EXTRACT_VALUE_INIT_JS, SMART_EXTRACT_INIT_JS
from settings import SETTLE_SLEEP_S, SETTLE_TIMEOUT_MS

log = logging.getLogger(__name__)

SMART_EXTRACT_CALL_JS = """(initJS) => {
    if (!window.__smartExtract) new Function(initJS)();
    return window.__smartExtract
        ? window.__smartExtract()
        : document.body.innerText;
}"""

EXTRACT_VALUE_CALL_JS = """([sel, initJS]) => {
    if (!window.__extractValue) new Function(initJS)();
    return window.__extractValue
        ? window.__extractValue(sel)
        : '[not found]';
}"""


@dataclass(slots=True)
class PageActionConfig:
    """Execution knobs shared across browser action call sites."""

    action_timeout_ms: int
    navigation_timeout_ms: int
    scroll_unit: int = 1
    type_delay_ms: int = 0
    settle_after_click: bool = True
    settle_after_evaluate: bool = True
    settle_timeout_ms: int = SETTLE_TIMEOUT_MS
    settle_sleep_s: float = SETTLE_SLEEP_S
    smart_body_extract: bool = True


@dataclass(slots=True)
class PageActionOutcome:
    """Normalized output from a primitive browser action."""

    text: str | None = None
    page_changed: bool = False
    navigation_status: int | str | None = None


async def execute_page_action(
    page: Any,
    action: str,
    params: dict[str, Any],
    *,
    config: PageActionConfig,
) -> PageActionOutcome:
    """Execute a primitive page action with shared semantics."""
    if action in ("type", "text"):
        action = "key_press"

    if action == "goto":
        url_before = page.url
        response = await page.goto(
            params["url"],
            wait_until="domcontentloaded",
            timeout=config.navigation_timeout_ms,
        )
        return PageActionOutcome(
            page_changed=page.url != url_before,
            navigation_status=response.status if response else "unknown",
        )

    if action == "click":
        url_before = page.url
        await page.click(params["selector"], timeout=config.action_timeout_ms)
        if config.settle_after_click:
            await wait_for_stable(page, config.settle_timeout_ms)
        return PageActionOutcome(page_changed=page.url != url_before)

    if action == "key_press":
        text = params.get("text")
        key = params.get("key")
        if not text and not key:
            raise ValueError("key_press requires 'text' and/or 'key'")
        parts: list[str] = []
        if text:
            selector = params.get("selector")
            if selector:
                await page.fill(selector, text, timeout=config.action_timeout_ms)
            else:
                await page.keyboard.type(text, delay=config.type_delay_ms)
            preview = text[:30] + "..." if len(text) > 30 else text
            parts.append(f"Typed '{preview}'")
        if key:
            await page.keyboard.press(key)
            parts.append(f"Pressed {key}")
        return PageActionOutcome(text=", ".join(parts))

    if action == "scroll":
        direction = params.get("direction", "down")
        amount = params.get("amount", 1)
        scaled_amount = amount * config.scroll_unit
        delta_x = 0
        delta_y = 0
        if direction == "down":
            delta_y = scaled_amount
        elif direction == "up":
            delta_y = -scaled_amount
        elif direction == "right":
            delta_x = scaled_amount
        elif direction == "left":
            delta_x = -scaled_amount
        await page.mouse.wheel(delta_x, delta_y)
        if config.settle_sleep_s > 0:
            await asyncio.sleep(config.settle_sleep_s)
        return PageActionOutcome(text=f"Scrolled {direction}")

    if action == "wait_for":
        selector = params["selector"]
        state = params.get("state", "visible")
        timeout = params.get("timeout_ms", config.action_timeout_ms)
        await page.wait_for_selector(selector, state=state, timeout=timeout)
        return PageActionOutcome(text=f"Element {selector} is {state}")

    if action == "select":
        await page.select_option(
            params["selector"],
            params.get("value", ""),
            timeout=config.action_timeout_ms,
        )
        return PageActionOutcome(text="Selected option")

    if action == "evaluate":
        script = params.get("script", "")
        if not script or not script.strip():
            log.warning("evaluate called with empty script — skipping")
            return PageActionOutcome()
        log.info("Executing JS evaluate (len=%d)", len(script))
        log.debug(
            "JS evaluate script: %.200s%s", script, "..." if len(script) > 200 else ""
        )
        url_before = page.url
        await page.evaluate(script)
        if config.settle_after_evaluate:
            await wait_for_stable(page, config.settle_timeout_ms)
        return PageActionOutcome(page_changed=page.url != url_before)

    if action == "extract":
        content = await extract_content(
            page,
            selector=params.get("selector", "body"),
            mode=params.get("mode", "text"),
            timeout_ms=config.action_timeout_ms,
            smart_body_extract=config.smart_body_extract,
        )
        return PageActionOutcome(text=content)

    raise ValueError(f"Unknown browser_dom action: {action}")


async def extract_content(
    page: Any,
    *,
    selector: str,
    mode: str,
    timeout_ms: int,
    smart_body_extract: bool,
) -> str:
    """Extract textual or HTML content using shared semantics."""
    is_body = selector.lower() in ("body", "html")
    if is_body and mode == "html":
        mode = "text"

    if mode == "html":
        return await page.inner_html(selector, timeout=timeout_ms)
    if mode == "value":
        return await page.evaluate(
            EXTRACT_VALUE_CALL_JS,
            [selector, EXTRACT_VALUE_INIT_JS],
        )
    if is_body and smart_body_extract:
        return await page.evaluate(
            SMART_EXTRACT_CALL_JS,
            SMART_EXTRACT_INIT_JS,
        )
    return await page.inner_text(selector, timeout=timeout_ms)


async def wait_for_stable(page: Any, timeout_ms: int) -> None:
    """Best-effort post-action stabilization shared by both execution paths."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
