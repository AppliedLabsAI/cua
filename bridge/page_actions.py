"""Shared primitive page actions used by both AI tools and playbook execution."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

from pydantic import BaseModel

from bridge.js_helpers import EXTRACT_VALUE_INIT_JS, READABILITY_EXTRACT_INIT_JS
from settings import AUTH_TYPE_DELAY_MS, PAGE_SETTLE_TIMEOUT_MS

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
    page_settle_timeout_ms: int = PAGE_SETTLE_TIMEOUT_MS


class PageActionOutcome(BaseModel):
    """Normalized output from a primitive browser action."""

    text: str | None = None
    page_changed: bool = False
    navigation_status: int | str | None = None


async def type_into_selector(
    page: Any,
    *,
    selector: str,
    text: str,
    timeout_ms: int,
    type_delay_ms: int,
) -> None:
    """Type into a focused field without using page.fill().

    This keeps auth interactions closer to real user input and avoids the
    immediate value injection pattern that login risk engines can flag.
    """
    await page.click(selector, timeout=timeout_ms)
    # Control+A: sandbox runs on Linux; Meta+A would be needed on macOS
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.keyboard.type(text, delay=type_delay_ms)


async def execute_page_action(
    page: Any,
    action: str,
    params: dict[str, Any],
    *,
    config: PageActionConfig,
    credentials: dict | None = None,
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
        await ensure_page_settled(page, config.page_settle_timeout_ms)
        return PageActionOutcome(
            page_changed=page.url != url_before,
            navigation_status=response.status if response else "unknown",
        )

    if action == "click":
        url_before = page.url
        await page.click(params["selector"], timeout=config.action_timeout_ms)
        await ensure_page_settled(page, config.page_settle_timeout_ms)
        return PageActionOutcome(page_changed=page.url != url_before)

    if action == "key_press":
        text = params.get("text")
        credential_ref = params.get("credential_ref")
        key = params.get("key")
        if text and credential_ref:
            raise ValueError(
                "key_press accepts either 'text' or 'credential_ref', not both"
            )
        if credential_ref:
            from credentials import resolve_credential_ref

            text = resolve_credential_ref(credentials, credential_ref)
        if not text and not key:
            raise ValueError(
                "key_press requires at least one of 'text', 'credential_ref', or 'key'"
            )
        parts: list[str] = []
        if text:
            selector = params.get("selector")
            delay = (
                config.type_delay_ms
                if not credential_ref
                else max(config.type_delay_ms, AUTH_TYPE_DELAY_MS)
            )
            if selector:
                await type_into_selector(
                    page,
                    selector=selector,
                    text=text,
                    timeout_ms=config.action_timeout_ms,
                    type_delay_ms=delay,
                )
            else:
                await page.keyboard.type(text, delay=delay)
            if credential_ref:
                parts.append(f"Typed credential '{credential_ref}'")
            else:
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
        await ensure_page_settled(page, config.page_settle_timeout_ms)
        return PageActionOutcome(page_changed=page.url != url_before)

    if action == "extract":
        selector = params.get("selector", "body")
        mode = params.get("mode", "markdown")
        # Auto-select text mode for body/html extracts: text mode is faster
        # (skips Readability+markdown pipeline), never truncated, and returns
        # complete content. Markdown is better for targeted selectors where
        # link/heading structure matters.
        is_body = selector.lower() in ("body", "html")
        if is_body and mode == "markdown":
            mode = "text"
        # Short settle for extract: content is already loaded by the time
        # the LLM decides to extract. A long wait is only needed after
        # navigation actions (goto, click).
        await ensure_page_settled(page, min(config.page_settle_timeout_ms, 2000))
        content = await extract_content(
            page,
            selector=selector,
            mode=mode,
            timeout_ms=config.action_timeout_ms,
        )
        return PageActionOutcome(text=content)

    raise ValueError(f"Unknown browser_dom action: {action}")


_HREF_EXACT_RE = __import__("re").compile(r'\[href="([^"]+)"\]')


def _relax_href_selector(selector: str) -> str | None:
    """Convert exact href matches to starts-with (^=) for truncated URLs.

    The DOM snapshot truncates hrefs (e.g. 60 chars).  When the LLM uses a
    truncated href as an exact-match CSS selector it won't find the element.
    Retrying with ``[href^="…"]`` handles this gracefully.
    """
    if _HREF_EXACT_RE.search(selector):
        return _HREF_EXACT_RE.sub(r'[href^="\1"]', selector)
    return None


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
    try:
        return await page.inner_text(selector, timeout=timeout_ms)
    except Exception:
        # Retry with starts-with match for truncated href selectors
        relaxed = _relax_href_selector(selector)
        if relaxed:
            logger.debug("Retrying extract with relaxed selector: %s", relaxed)
            return await page.inner_text(relaxed, timeout=timeout_ms)
        raise


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


async def ensure_page_settled(
    page: Any, timeout_ms: int = PAGE_SETTLE_TIMEOUT_MS
) -> None:
    """Wait until the page's network activity has settled.

    Uses Playwright's networkidle signal (500ms of no in-flight requests).
    Never raises — silently degrades on timeout.
    """
    if timeout_ms <= 0:
        return
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
