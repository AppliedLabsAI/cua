"""DOM-based browser executor using Patchright.

Manages a Patchright Chromium instance running headful on Xvfb (DISPLAY=:99),
visible in the noVNC stream. Provides precise, fast browser interactions via
CSS/text/role selectors — no pixel-hunting needed.

Patchright handles bot detection evasion natively. Do NOT add extra stealth
flags — they can conflict with its internal CDP patches.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import Any, cast

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from bridge import DOM_MARKER, ActionResult

_DEFAULT_TIMEOUT = 3000  # 3s for all actions — fail fast on bad selectors
_DOM_MAX_CHARS = 3500
# Compact DOM auto-attached to goto/click responses
_AUTO_DOM_MAX_CHARS = 2500  # Leave room for nav text + content summary

# --- JS files loaded from disk once at import time ---
_JS_DIR = Path(__file__).parent / "scripts"
_DOM_SNAPSHOT_INIT_JS = (_JS_DIR / "dom_snapshot.js").read_text()
_SMART_EXTRACT_INIT_JS = (_JS_DIR / "smart_extract.js").read_text()
_CAPTCHA_DETECT_INIT_JS = (_JS_DIR / "captcha_detect.js").read_text()
_EXTRACT_VALUE_INIT_JS = (_JS_DIR / "extract_value.js").read_text()


class BrowserManager:
    """Manages a Patchright Chromium browser lifecycle on the virtual display."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def launch(
        self,
        width: int,
        height: int,
        start_url: str | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Launch Patchright Chromium headful on the Xvfb display."""
        self._playwright = await async_playwright().start()

        launch_args = [
            f"--window-size={width},{height}",
            "--window-position=0,0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=launch_args,
        )

        context_kwargs: dict = {
            "viewport": {"width": width, "height": height},
        }
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}
        if user_agent:
            context_kwargs["user_agent"] = user_agent

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(_DEFAULT_TIMEOUT)

        # Pre-load JS helpers on every page (survives navigations)
        await self._context.add_init_script(script=_DOM_SNAPSHOT_INIT_JS)
        await self._context.add_init_script(script=_SMART_EXTRACT_INIT_JS)
        await self._context.add_init_script(script=_CAPTCHA_DETECT_INIT_JS)
        await self._context.add_init_script(script=_EXTRACT_VALUE_INIT_JS)

        self._page = await self._context.new_page()

        if start_url:
            await self._page.goto(start_url, wait_until="domcontentloaded")

    @property
    def page(self) -> Page:
        """Current active page. Raises if browser not launched."""
        if self._page is None:
            raise RuntimeError("Browser not launched — call launch() first")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser not launched — call launch() first")
        return self._context

    async def close(self) -> None:
        """Shut down browser and Patchright.

        Tolerates already-dead connections (e.g. after Ctrl+C).
        """
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass  # Browser process may already be dead
        finally:
            self._browser = None
            self._context = None
            self._page = None
            try:
                if self._playwright:
                    await self._playwright.stop()
            except Exception:
                pass  # Connection may be closed
            finally:
                self._playwright = None


async def _ensure_dom_snapshot(page: Page) -> None:
    """Re-inject __domSnapshot if the init script hasn't loaded on this page."""
    await page.evaluate(_DOM_SNAPSHOT_INIT_JS)


async def quick_dom_snapshot(
    page: Page,
    max_chars: int = _AUTO_DOM_MAX_CHARS,
    filter_config: dict | None = None,
) -> str:
    """Fast DOM snapshot using pre-loaded __domSnapshot init script.

    When filter_config is provided (from Cognitive Blinders), it is passed
    as the 3rd argument to window.__domSnapshot for JS-side filtering.
    """
    try:
        raw = await page.evaluate(
            "([s, m, f]) => window.__domSnapshot ? window.__domSnapshot(s, m, f) : null",
            [None, max_chars, filter_config],
        )
        if raw is None:
            await _ensure_dom_snapshot(page)
            raw = await page.evaluate(
                "([s, m, f]) => window.__domSnapshot(s, m, f)",
                [None, max_chars, filter_config],
            )
        data = json.loads(raw)
        return f"[{data['title']}] {data['url']}\n{data['dom']}"
    except Exception:
        return ""


async def page_screenshot(page: Page) -> str:
    """Capture the browser viewport as base64 JPEG via Playwright.

    JPEG is ~3-5x smaller than PNG, reducing image tokens sent to Claude.
    Quality 55 is sufficient for DOM-based UI understanding.
    """
    jpeg_bytes = await page.screenshot(type="jpeg", quality=55)
    return base64.standard_b64encode(jpeg_bytes).decode("ascii")


async def execute_dom_action(
    action: str,
    params: dict,
    browser: BrowserManager,
    *,
    _skip_screenshot: bool = False,
    filter_config: dict | None = None,
) -> ActionResult:
    """Execute a browser_dom tool action via Patchright.

    When _skip_screenshot=True (used by execute_sequence for intermediate steps),
    goto/click/scroll skip the screenshot capture for speed.
    """
    # Normalize common action aliases
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
            text = None
            if dom:
                text = f"{DOM_MARKER}\n{dom}"
            return ActionResult(screenshot_b64=b64, text=text)

        elif action == "goto":
            url = params["url"]
            resp = await page.goto(
                url, wait_until="domcontentloaded", timeout=_DEFAULT_TIMEOUT
            )
            status = resp.status if resp else "unknown"
            if _skip_screenshot:
                return ActionResult(text=f"Navigated to {url} (status {status})")
            nav_text = f"Navigated to {url} (status {status})"
            # Default to DOM-only for goto — saves ~1500 image tokens per navigation.
            # Agent can use screenshot action when visual context is needed.
            dom = await quick_dom_snapshot(page, filter_config=filter_config)
            if dom:
                nav_text += f"\n\n{DOM_MARKER}\n{dom}"
            return ActionResult(text=nav_text)

        elif action == "click":
            selector = params["selector"]
            url_before = page.url
            await page.click(selector, timeout=_DEFAULT_TIMEOUT)
            if _skip_screenshot:
                return ActionResult(text="Clicked")
            # If click triggered navigation, wait for new page to load
            if page.url != url_before:
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
            # Default to DOM-only — saves ~1500 image tokens per click.
            # Agent can use screenshot action when visual context is needed.
            click_text = "Clicked"
            dom = await quick_dom_snapshot(page, filter_config=filter_config)
            if dom:
                click_text += f"\n\n{DOM_MARKER}\n{dom}"
            return ActionResult(text=click_text)

        elif action == "key_press":
            text = params.get("text", "")
            key = params.get("key", "")
            if not text and not key:
                return ActionResult(error="key_press requires 'text' and/or 'key'")
            parts: list[str] = []
            if text:
                await page.keyboard.type(text)
                preview = text[:30] + "..." if len(text) > 30 else text
                parts.append(f"Typed '{preview}'")
            if key:
                await page.keyboard.press(key)
                parts.append(f"Pressed {key}")
            return ActionResult(text=", ".join(parts))

        elif action == "scroll":
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            delta_x, delta_y = 0, 0
            if direction == "down":
                delta_y = amount * 200
            elif direction == "up":
                delta_y = -(amount * 200)
            elif direction == "right":
                delta_x = amount * 200
            elif direction == "left":
                delta_x = -(amount * 200)
            await page.mouse.wheel(delta_x, delta_y)
            if _skip_screenshot:
                return ActionResult(text=f"Scrolled {direction}")
            scroll_text = f"Scrolled {direction}"
            if dom_only:
                return ActionResult(text=scroll_text)
            # Scroll doesn't change DOM — skip DOM snapshot, only take screenshot
            b64 = await page_screenshot(page)
            return ActionResult(screenshot_b64=b64, text=scroll_text)

        elif action == "extract":
            selector = params.get("selector", "body")
            mode = params.get("mode", "text")
            # Smart extract: scope 'body' to main content area
            is_body = selector.lower() in ("body", "html")
            if is_body and mode == "html":
                # HTML mode on body is a token bomb — downgrade to text
                mode = "text"
            if mode == "html":
                content = await page.inner_html(selector, timeout=_DEFAULT_TIMEOUT)
            elif mode == "value":
                # Use __extractValue if available, inline fallback if init script lost
                content = await page.evaluate(
                    """(sel) => {
                        if (window.__extractValue) return window.__extractValue(sel);
                        const el = document.querySelector(sel);
                        if (!el) return '[not found]';
                        const tag = el.tagName.toLowerCase();
                        if (tag === 'select') { const o = el.options?.[el.selectedIndex]; return o ? o.text.trim() : ''; }
                        if (tag === 'textarea' || tag === 'input') return el.value;
                        return el.innerText || el.textContent || '';
                    }""",
                    selector,
                )
            elif is_body:
                # Smart body extract via pre-loaded script or fallback
                content = await page.evaluate(
                    "() => window.__smartExtract"
                    " ? window.__smartExtract()"
                    " : document.body.innerText"
                )
            else:
                content = await page.inner_text(selector, timeout=_DEFAULT_TIMEOUT)
            return ActionResult(text=content)

        elif action == "get_dom":
            selector = params.get("selector")
            raw = await page.evaluate(
                "([s, m, f]) => window.__domSnapshot ? window.__domSnapshot(s, m, f) : null",
                [selector, _DOM_MAX_CHARS, filter_config],
            )
            if raw is None:
                await _ensure_dom_snapshot(page)
                raw = await page.evaluate(
                    "([s, m, f]) => window.__domSnapshot(s, m, f)",
                    [selector, _DOM_MAX_CHARS, filter_config],
                )
            data = json.loads(raw)
            header = f"[{data['title']}] {data['url']}\n"
            return ActionResult(text=header + data["dom"])

        elif action == "wait_for":
            selector = params["selector"]
            state = params.get("state", "visible")
            await page.wait_for_selector(
                selector, state=state, timeout=_DEFAULT_TIMEOUT
            )
            return ActionResult(text=f"Element {selector} is {state}")

        elif action == "execute_sequence":
            return await _execute_sequence(params, browser, filter_config=filter_config)

        else:
            return ActionResult(error=f"Unknown browser_dom action: {action}")

    except TimeoutError:
        return ActionResult(
            error=f"{action} timed out after {_DEFAULT_TIMEOUT // 1000}s"
        )
    except Exception as e:
        return ActionResult(error=f"browser_dom.{action} failed: {e}")


async def _execute_sequence(
    params: dict,
    browser: BrowserManager,
    filter_config: dict | None = None,
) -> ActionResult:
    """Execute a sequence of browser actions in one tool call.

    Runs each step sequentially, collecting results. Stops on first error
    and returns partial results up to the failure point.
    """
    steps = params.get("steps")
    if not steps or not isinstance(steps, list):
        return ActionResult(error="execute_sequence requires a 'steps' array")

    results: list[str] = []
    last_step = len(steps) - 1
    for i, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            return ActionResult(
                error=f"Step {i + 1}: missing 'action'",
                text="\n".join(results) if results else None,
            )

        step = cast(dict[str, Any], raw_step)
        action = step.get("action")
        if not isinstance(action, str) or not action:
            return ActionResult(
                error=f"Step {i + 1}: missing 'action'",
                text="\n".join(results) if results else None,
            )
        if action == "execute_sequence":
            return ActionResult(
                error=f"Step {i + 1}: nested execute_sequence not allowed",
                text="\n".join(results) if results else None,
            )

        # Intermediate steps skip screenshots for speed; last step runs normally.
        # goto/click always return DOM-only, so intermediate nav still has context.
        is_last = i == last_step
        result = await execute_dom_action(
            action,
            step,
            browser,
            _skip_screenshot=not is_last,
            filter_config=filter_config,
        )

        if result.error:
            # On error, take a screenshot so the agent can see what went wrong
            try:
                b64 = await page_screenshot(browser.page)
            except Exception:
                b64 = None
            return ActionResult(
                screenshot_b64=b64,
                text="\n".join(results) if results else None,
                error=f"Step {i + 1} ({action}): {result.error}",
            )

        results.append(f"Step {i + 1} ({action}): {result.text or 'OK'}")

    # Last step already has screenshot + DOM (if it was goto/click).
    # Only attach DOM if the last step didn't already include it.
    combined_text = "\n".join(results)
    if DOM_MARKER not in (result.text or ""):
        dom = await quick_dom_snapshot(browser.page, filter_config=filter_config)
        if dom:
            combined_text += f"\n\n{DOM_MARKER}\n{dom}"
    return ActionResult(
        screenshot_b64=result.screenshot_b64,
        text=combined_text,
    )
