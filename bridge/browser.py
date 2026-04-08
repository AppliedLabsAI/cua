"""DOM-based browser executor using Patchright.

Manages a Patchright Chromium instance running headful on Xvfb (DISPLAY=:99).
Provides precise, fast browser interactions via CSS/text/role selectors — no
pixel-hunting needed. Sessions are recorded via Playwright tracing for replay.

Patchright handles core bot-detection evasion (navigator.webdriver, etc.).
On top of that, this module injects additional anti-fingerprint JS evasions
and uses enhanced Chrome launch args to reduce automation signals.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from bridge.js_helpers import (
    CAPTCHA_DETECT_INIT_JS,
    EXTRACT_VALUE_INIT_JS,
    PAGE_CONTEXT_INIT_JS,
    READABILITY_EXTRACT_INIT_JS,
    STEALTH_EVASIONS_INIT_JS,
)
from bridge.page_actions import ensure_page_settled
from bridge.stealth import get_stealth_launch_args
from settings import (
    ACTION_TIMEOUT_MS,
    NAVIGATION_TIMEOUT_MS,
    PAGE_SETTLE_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

DOM_MAX_CHARS = 4000
# Compact DOM auto-attached to goto/click responses
AUTO_DOM_MAX_CHARS = 3000  # Leave room for nav text + content summary
# Smaller DOM for extract results — content is already in the text
EXTRACT_DOM_MAX_CHARS = 1500


class BrowserManager:
    """Manages a Patchright Chromium browser lifecycle on the virtual display.

    Stealth evasions (anti-bot launch args + JS fingerprint masking) are
    always active. CAPTCHA solving uses active click strategies ported
    from SeleniumBase's CDP Mode.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._prefetch_task: asyncio.Task[str] | None = None
        self._prefetch_url: str | None = None
        self._settle_task: asyncio.Task[None] | None = None

    async def launch(
        self,
        width: int,
        height: int,
        start_url: str | None = None,
        proxy: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Launch Patchright Chromium headful on the Xvfb display.

        Uses enhanced launch args (from SeleniumBase's Config) and injects
        stealth JS evasions before any page scripts execute.
        """
        self._playwright = await async_playwright().start()

        launch_args = get_stealth_launch_args(width, height)

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
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)

        # Stealth JS evasions — must be injected FIRST so they run before
        # any page script (including anti-bot detectors).
        if STEALTH_EVASIONS_INIT_JS:
            await self._context.add_init_script(script=STEALTH_EVASIONS_INIT_JS)

        # Pre-load JS helpers on every page (survives navigations).
        # Sequential: Playwright serializes CDP commands on one socket anyway.
        await self._context.add_init_script(script=PAGE_CONTEXT_INIT_JS)
        await self._context.add_init_script(script=CAPTCHA_DETECT_INIT_JS)
        await self._context.add_init_script(script=EXTRACT_VALUE_INIT_JS)
        await self._context.add_init_script(script=READABILITY_EXTRACT_INIT_JS)

        self._page = await self._context.new_page()

        # Auto-follow new tabs / popups so the agent operates on the latest page.
        # Init scripts are context-level, so JS helpers are already available.
        self._context.on("page", self._on_new_page)

        if start_url:
            await self._page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            await ensure_page_settled(self._page, NAVIGATION_TIMEOUT_MS)

    def _on_new_page(self, page: Page) -> None:
        """Handle new tab / popup: switch the active page automatically."""
        prev_url = self._page.url if self._page else "none"
        self._page = page
        if self._settle_task and not self._settle_task.done():
            self._settle_task.cancel()
        self._settle_task = asyncio.create_task(
            ensure_page_settled(page, PAGE_SETTLE_TIMEOUT_MS)
        )
        logger.info(
            "Switched to new tab: %s (from %s)",
            page.url[:80] or "about:blank",
            prev_url[:80],
        )

    async def wait_for_active_page(self) -> None:
        """Await any in-flight settle for the current active page."""
        task = self._settle_task
        if task is None or task.done():
            return
        with contextlib.suppress(Exception):
            await asyncio.shield(task)

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

    # ------------------------------------------------------------------
    # CAPTCHA solving
    # ------------------------------------------------------------------

    async def solve_captcha(self) -> str | None:
        """Actively attempt to solve any CAPTCHA on the current page.

        Uses SeleniumBase's click strategies ported in captcha_solver.py.
        Returns a status message if solved, None if no CAPTCHA found.
        """
        from bridge.captcha_solver import handle_captcha_with_active_solving

        result = await handle_captcha_with_active_solving(self.page)
        if result.detected:
            if result.resolved:
                return result.message
            logger.warning("CAPTCHA not resolved: %s", result.message)
            return result.message
        return None

    # ------------------------------------------------------------------
    # Speculative prefetch
    # ------------------------------------------------------------------

    def start_prefetch(self, coro: Coroutine[Any, Any, str]) -> None:
        """Start a speculative page map fetch in the background.

        The task is consumed by consume_prefetch() if the URL still matches.
        """
        # Cancel any stale prefetch
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_url = self.page.url
        self._prefetch_task = asyncio.create_task(coro)

    async def consume_prefetch(self) -> str:
        """Await and return the prefetched result if URL still matches.

        Returns empty string if no prefetch, URL mismatch, or error.
        Clears the prefetch state after consumption.
        """
        task = self._prefetch_task
        url = self._prefetch_url
        self._prefetch_task = None
        self._prefetch_url = None

        if task and url == self.page.url:
            try:
                return await task
            except Exception:
                return ""
        if task and not task.done():
            task.cancel()
        return ""

    async def close(self) -> None:
        """Shut down browser and Patchright.

        Tolerates already-dead connections (e.g. after Ctrl+C).
        """
        # Cancel any in-flight prefetch
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetch_url = None
        if self._settle_task and not self._settle_task.done():
            self._settle_task.cancel()
        self._settle_task = None

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            logger.debug("Browser close failed (process may be dead)", exc_info=True)
        finally:
            self._browser = None
            self._context = None
            self._page = None
            try:
                if self._playwright:
                    await self._playwright.stop()
            except Exception:
                logger.debug(
                    "Playwright stop failed (connection may be closed)", exc_info=True
                )
            finally:
                self._playwright = None
