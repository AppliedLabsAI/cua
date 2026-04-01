"""DOM-based browser executor using Patchright.

Manages a Patchright Chromium instance running headful on Xvfb (DISPLAY=:99).
Provides precise, fast browser interactions via CSS/text/role selectors — no
pixel-hunting needed. Sessions are recorded via Playwright tracing for replay.

Patchright handles bot detection evasion natively. Do NOT add extra stealth
flags — they can conflict with its internal CDP patches.
"""

from __future__ import annotations

import asyncio
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
)
from settings import ACTION_TIMEOUT_MS, NAVIGATION_TIMEOUT_MS

logger = logging.getLogger(__name__)

DOM_MAX_CHARS = 4000
# Compact DOM auto-attached to goto/click responses
AUTO_DOM_MAX_CHARS = 3000  # Leave room for nav text + content summary


class BrowserManager:
    """Manages a Patchright Chromium browser lifecycle on the virtual display."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._prefetch_task: asyncio.Task[str] | None = None
        self._prefetch_url: str | None = None

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
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)

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

    def _on_new_page(self, page: Page) -> None:
        """Handle new tab / popup: switch the active page automatically."""
        prev_url = self._page.url if self._page else "none"
        self._page = page
        logger.info(
            "Switched to new tab: %s (from %s)",
            page.url[:80] or "about:blank",
            prev_url[:80],
        )

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
