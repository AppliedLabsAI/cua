"""Page observation and DOM snapshot helpers."""

from __future__ import annotations

import base64
import contextlib
import json
from typing import TYPE_CHECKING

from patchright.async_api import Page

from bridge import DOM_MARKER
from bridge.browser import _AUTO_DOM_MAX_CHARS
from bridge.js_helpers import PAGE_CONTEXT_INIT_JS

if TYPE_CHECKING:
    from bridge.browser import BrowserManager

_DOM_SNAPSHOT_CALL_JS = """([s, m, f, initJS]) => {
    if (!window.__domSnapshot) new Function(initJS)();
    return window.__domSnapshot ? window.__domSnapshot(s, m, f) : null;
}"""

_PAGE_MAP_CALL_JS = """([m, f, initJS]) => {
    if (!window.__pageMap) new Function(initJS)();
    return window.__pageMap ? window.__pageMap(m, f) : null;
}"""

_START_OBSERVING_JS = """([initJS]) => {
    if (!window.__startObserving) new Function(initJS)();
    if (window.__startObserving) window.__startObserving();
}"""

_STOP_OBSERVING_JS = """([initJS]) => {
    if (!window.__stopObserving) new Function(initJS)();
    return window.__stopObserving ? window.__stopObserving() : null;
}"""


async def quick_dom_snapshot(
    page: Page,
    max_chars: int = _AUTO_DOM_MAX_CHARS,
    filter_config: dict | None = None,
) -> str:
    """Fast DOM snapshot via a self-healing page helper."""
    try:
        raw = await page.evaluate(
            _DOM_SNAPSHOT_CALL_JS,
            [None, max_chars, filter_config, PAGE_CONTEXT_INIT_JS],
        )
        if raw is None:
            return ""
        data = json.loads(raw)
        return f"[{data['title']}] {data['url']}\n{data['dom']}"
    except Exception:
        return ""


async def get_dom_snapshot(
    page: Page,
    *,
    selector: str | None,
    max_chars: int,
    filter_config: dict | None = None,
) -> str | None:
    """Return a targeted DOM snapshot payload, or ``None`` if unavailable."""
    try:
        raw = await page.evaluate(
            _DOM_SNAPSHOT_CALL_JS,
            [selector, max_chars, filter_config, PAGE_CONTEXT_INIT_JS],
        )
    except Exception:
        return None
    if raw is None:
        return None
    data = json.loads(raw)
    header = f"[{data['title']}] {data['url']}\n"
    return header + data["dom"]


async def quick_axtree_snapshot(
    page: Page, max_chars: int = _AUTO_DOM_MAX_CHARS
) -> str:
    """Accessibility tree snapshot fallback."""
    try:
        snapshot = await page.locator("body").aria_snapshot()
        if not snapshot:
            return ""
        title = await page.title()
        url = page.url
        header = f"[{title}] {url}\n"
        if len(snapshot) > max_chars:
            snapshot = snapshot[:max_chars] + "\n[...truncated]"
        return header + snapshot
    except Exception:
        return ""


async def quick_page_map(
    page: Page,
    max_chars: int = 6000,
    filter_config: dict | None = None,
) -> str:
    """Full page action map for follow-up model steps."""
    try:
        raw = await page.evaluate(
            _PAGE_MAP_CALL_JS,
            [max_chars, filter_config, PAGE_CONTEXT_INIT_JS],
        )
        if raw is None:
            return ""
        data = json.loads(raw)
        return data["map"]
    except Exception:
        return ""


async def attach_page_context(
    browser: BrowserManager,
    filter_config: dict | None = None,
) -> str:
    """Attach the best available page context with the DOM marker prefix."""
    page = browser.page
    ctx = await browser.consume_prefetch()
    if not ctx:
        ctx = await quick_page_map(page, filter_config=filter_config)
    if not ctx:
        ctx = await quick_axtree_snapshot(page)
    if ctx:
        return f"\n\n{DOM_MARKER}\n{ctx}"
    return ""


async def start_mutation_observer(page: Page) -> None:
    """Start DOM Mutation Observer before an action."""
    with contextlib.suppress(Exception):
        await page.evaluate(_START_OBSERVING_JS, [PAGE_CONTEXT_INIT_JS])


async def stop_mutation_observer(page: Page) -> str:
    """Stop observer and return a compact change summary."""
    try:
        summary = await page.evaluate(_STOP_OBSERVING_JS, [PAGE_CONTEXT_INIT_JS])
        return f"[{summary}]" if summary else ""
    except Exception:
        return ""


async def page_screenshot(page: Page) -> str:
    """Capture the browser viewport as base64 JPEG via Playwright."""
    jpeg_bytes = await page.screenshot(type="jpeg", quality=55)
    return base64.standard_b64encode(jpeg_bytes).decode("ascii")
