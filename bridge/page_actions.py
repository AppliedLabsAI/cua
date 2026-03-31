"""Shared primitive page actions used by both AI tools and playbook execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from pydantic import BaseModel

from bridge.js_helpers import EXTRACT_VALUE_INIT_JS, READABILITY_EXTRACT_INIT_JS
from settings import SETTLE_SLEEP_S, SETTLE_TIMEOUT_MS

logger = logging.getLogger(__name__)

EXTRACT_VALUE_CALL_JS = """([sel, initJS]) => {
    if (!window.__extractValue) new Function(initJS)();
    return window.__extractValue
        ? window.__extractValue(sel)
        : '[not found]';
}"""

READABILITY_EXTRACT_CALL_JS = """(initJS) => {
    if (!window.__readabilityExtract) new Function(initJS)();
    return window.__readabilityExtract
        ? window.__readabilityExtract()
        : null;
}"""


class PageActionConfig(BaseModel):
    """Execution knobs shared across browser action call sites."""

    action_timeout_ms: int
    navigation_timeout_ms: int
    scroll_unit: int = 1
    type_delay_ms: int = 0
    settle_after_click: bool = True
    settle_after_evaluate: bool = True
    settle_timeout_ms: int = SETTLE_TIMEOUT_MS
    settle_sleep_s: float = SETTLE_SLEEP_S


class PageActionOutcome(BaseModel):
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
            logger.warning("evaluate called with empty script — skipping")
            return PageActionOutcome()
        logger.info("Executing JS evaluate (len=%d)", len(script))
        logger.debug(
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
            mode=params.get("mode", "markdown"),
            timeout_ms=config.action_timeout_ms,
        )
        return PageActionOutcome(text=content)

    raise ValueError(f"Unknown browser_dom action: {action}")


async def extract_content(
    page: Any,
    *,
    selector: str,
    mode: str,
    timeout_ms: int,
) -> str:
    """Extract textual or HTML content using shared semantics."""
    is_body = selector.lower() in ("body", "html")
    if is_body and mode == "html":
        mode = "text"

    if mode == "markdown" and is_body:
        return await _extract_markdown(page)

    if mode == "html":
        return await page.inner_html(selector, timeout=timeout_ms)
    if mode == "value":
        return await page.evaluate(
            EXTRACT_VALUE_CALL_JS,
            [selector, EXTRACT_VALUE_INIT_JS],
        )
    return await page.inner_text(selector, timeout=timeout_ms)


async def _extract_markdown(page: Any) -> str:
    """Readability extraction → markdown conversion pipeline."""
    raw = await page.evaluate(
        READABILITY_EXTRACT_CALL_JS,
        READABILITY_EXTRACT_INIT_JS,
    )
    if raw:
        try:
            data = json.loads(raw)
            from bridge.markdown import html_to_markdown, truncate_markdown

            md = html_to_markdown(data["html"], base_url=data.get("url", ""))
            return truncate_markdown(md)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Markdown extraction parse error: %s", exc)
        except Exception:
            logger.warning(
                "Markdown extraction failed, falling back to innerText",
                exc_info=True,
            )
    # Fallback to plain innerText
    return await page.inner_text("body")


async def wait_for_stable(page: Any, timeout_ms: int) -> None:
    """Best-effort post-action stabilization shared by both execution paths."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
