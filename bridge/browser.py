"""DOM-based browser executor using Patchright.

Manages a Patchright Chromium instance running headful on Xvfb (DISPLAY=:99).
Provides precise, fast browser interactions via CSS/text/role selectors — no
pixel-hunting needed. Sessions are recorded via Playwright tracing for replay.

Patchright handles bot detection evasion natively. Do NOT add extra stealth
flags — they can conflict with its internal CDP patches.
"""

from __future__ import annotations

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from bridge.js_helpers import (
    CAPTCHA_DETECT_INIT_JS,
    DOM_SNAPSHOT_INIT_JS,
    EXTRACT_VALUE_INIT_JS,
    SMART_EXTRACT_INIT_JS,
)

_DEFAULT_TIMEOUT = 3000  # 3s for clicks/waits/selectors — fail fast on bad selectors
_NAVIGATION_TIMEOUT = 7_000  # 7s for page loads — real sites need more time
_DOM_MAX_CHARS = 3500
# Compact DOM auto-attached to goto/click responses
_AUTO_DOM_MAX_CHARS = 2500  # Leave room for nav text + content summary


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
        await self._context.add_init_script(script=DOM_SNAPSHOT_INIT_JS)
        await self._context.add_init_script(script=SMART_EXTRACT_INIT_JS)
        await self._context.add_init_script(script=CAPTCHA_DETECT_INIT_JS)
        await self._context.add_init_script(script=EXTRACT_VALUE_INIT_JS)

        self._page = await self._context.new_page()

        if start_url:
            await self._page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT,
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
