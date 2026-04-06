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

PAGE_READINESS_CALL_JS = """(selector) => {
    const probeSelector = selector || 'body';
    let target = null;
    try {
        target = document.querySelector(probeSelector);
    } catch (_) {
        target = null;
    }

    const probe = target || document.body || document.documentElement;
    const text = (
        probe?.innerText ??
        probe?.textContent ??
        probe?.value ??
        ''
    ).trim();

    const busySelectors = [
        "[aria-busy='true']",
        "[role='progressbar']",
        "[data-loading='true']",
        "[data-testid*='loading']",
        ".loading",
        ".loader",
        ".spinner",
        ".skeleton",
    ];

    let busyCount = 0;
    for (const sel of busySelectors) {
        try {
            busyCount += document.querySelectorAll(sel).length;
        } catch (_) {
            // Ignore invalid selectors and continue probing readiness.
        }
    }

    return {
        readyState: document.readyState || '',
        selectorMatched: Boolean(target),
        textLength: text.length,
        busyCount,
    };
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
        await wait_for_stable(
            page,
            config.settle_timeout_ms,
            settle_sleep_s=config.settle_sleep_s,
        )
        return PageActionOutcome(
            page_changed=page.url != url_before,
            navigation_status=response.status if response else "unknown",
        )

    if action == "click":
        url_before = page.url
        await page.click(params["selector"], timeout=config.action_timeout_ms)
        if config.settle_after_click:
            await wait_for_stable(
                page,
                config.settle_timeout_ms,
                settle_sleep_s=config.settle_sleep_s,
            )
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
            if selector:
                await page.fill(selector, text, timeout=config.action_timeout_ms)
            else:
                await page.keyboard.type(text, delay=config.type_delay_ms)
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
            await wait_for_stable(
                page,
                config.settle_timeout_ms,
                settle_sleep_s=config.settle_sleep_s,
            )
        return PageActionOutcome(page_changed=page.url != url_before)

    if action == "extract":
        selector = params.get("selector", "body")
        await wait_for_page_ready(
            page,
            config.settle_timeout_ms,
            selector=selector,
            wait_for_content=True,
            settle_sleep_s=config.settle_sleep_s,
        )
        content = await extract_content(
            page,
            selector=selector,
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


async def _get_page_readiness_snapshot(
    page: Any,
    *,
    selector: str | None,
) -> dict[str, Any] | None:
    try:
        snapshot = await page.evaluate(PAGE_READINESS_CALL_JS, selector)
    except Exception:
        return None
    if not isinstance(snapshot, dict):
        return None
    return snapshot


async def wait_for_page_ready(
    page: Any,
    timeout_ms: int,
    *,
    selector: str | None = None,
    wait_for_content: bool = False,
    settle_sleep_s: float = SETTLE_SLEEP_S,
) -> None:
    """Best-effort readiness wait that works for both static and SPA pages."""
    if timeout_ms <= 0:
        return

    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load", timeout=min(timeout_ms, 1_500))
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 1_500))

    if selector:
        with contextlib.suppress(Exception):
            await page.wait_for_selector(
                selector,
                state="visible",
                timeout=min(timeout_ms, 1_000),
            )

    if not wait_for_content:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + (timeout_ms / 1000)
    last_signature: tuple[Any, ...] | None = None
    stable_polls = 0
    sleep_interval = settle_sleep_s if settle_sleep_s > 0 else 0.05

    while loop.time() < deadline:
        snapshot = await _get_page_readiness_snapshot(page, selector=selector)
        if snapshot is None:
            return

        signature = (
            snapshot.get("readyState"),
            snapshot.get("textLength"),
            snapshot.get("busyCount"),
            snapshot.get("selectorMatched"),
        )
        has_content = bool(snapshot.get("textLength", 0))
        ready_state = snapshot.get("readyState") in {"interactive", "complete"}
        busy_count = int(snapshot.get("busyCount", 0) or 0)

        if signature == last_signature and has_content:
            stable_polls += 1
        else:
            stable_polls = 0
        last_signature = signature

        required_stable_polls = 1 if busy_count == 0 else 2

        if has_content and ready_state and stable_polls >= required_stable_polls:
            return

        await asyncio.sleep(sleep_interval)


async def wait_for_stable(
    page: Any,
    timeout_ms: int,
    *,
    settle_sleep_s: float = SETTLE_SLEEP_S,
) -> None:
    """Best-effort post-action stabilization shared by both execution paths."""
    await wait_for_page_ready(
        page,
        timeout_ms,
        selector="body",
        wait_for_content=True,
        settle_sleep_s=settle_sleep_s,
    )
