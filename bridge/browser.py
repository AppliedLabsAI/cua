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
import hashlib
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from bridge.fingerprint import build_fingerprint_js, generate_fingerprint
from bridge.js_helpers import (
    CAPTCHA_DETECT_INIT_JS,
    EXTRACT_VALUE_INIT_JS,
    PAGE_CONTEXT_INIT_JS,
    READABILITY_EXTRACT_INIT_JS,
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
        self._storage_state_path: str | None = None

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

        # Generate a realistic browser fingerprint in sandbox environments
        # (Linux VM with generic hardware). Skip locally where the real
        # fingerprint is genuine and spoofing causes detectable mismatches.
        fp: dict | None = None
        in_sandbox = Path("/recordings").exists()
        if in_sandbox:
            fp = generate_fingerprint(width, height, start_url=start_url)

        context_kwargs: dict = {
            "viewport": {"width": width, "height": height},
        }
        if fp:
            context_kwargs["user_agent"] = user_agent or fp["userAgent"]
        elif user_agent:
            context_kwargs["user_agent"] = user_agent
        if proxy:
            context_kwargs["proxy"] = _parse_proxy(proxy)

        # Restore cookies/localStorage keyed by start_url domain so
        # hCaptcha passes and auth sessions persist across runs.
        self._storage_state_path = _storage_path_for_url(start_url)
        if self._storage_state_path and Path(self._storage_state_path).exists():
            context_kwargs["storage_state"] = self._storage_state_path
            logger.info("Restored storage state from %s", self._storage_state_path)

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)

        # When using a proxy, set timezone and geolocation to match the
        # proxy IP's region and block WebRTC from leaking the real IP.
        if proxy:
            await self._apply_proxy_protections(proxy)

        # Inject fingerprint JS in sandbox mode only.
        if fp:
            fp_js = build_fingerprint_js(fp)
            await self._context.add_init_script(script=fp_js)

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

    async def _apply_proxy_protections(self, proxy: str) -> None:
        """Prevent IP/timezone leaks when using a proxy.

        hCaptcha cross-checks:
        1. WebRTC STUN requests (reveals real IP behind proxy)
        2. JS timezone vs proxy IP geolocation
        3. navigator.language vs expected locale

        We use CDP to set timezone and block WebRTC leaks.
        """
        assert self._context is not None  # Called right after context creation
        ctx = self._context

        # Block WebRTC IP leak via CDP
        try:
            cdp = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await cdp.evaluate("""
                // Override RTCPeerConnection to prevent IP leak via STUN
                const origRTC = window.RTCPeerConnection;
                window.RTCPeerConnection = function(...args) {
                    const config = args[0] || {};
                    // Remove all STUN/TURN servers
                    config.iceServers = [];
                    return new origRTC(config);
                };
                window.RTCPeerConnection.prototype = origRTC.prototype;
                // Also block via webkitRTCPeerConnection
                if (window.webkitRTCPeerConnection) {
                    window.webkitRTCPeerConnection = window.RTCPeerConnection;
                }
            """)
        except Exception:
            logger.debug("WebRTC override via evaluate failed", exc_info=True)

        # Inject WebRTC block as init script so it persists across navigations
        await ctx.add_init_script(
            script="""
            (() => {
                const origRTC = window.RTCPeerConnection;
                if (!origRTC) return;
                window.RTCPeerConnection = function(...args) {
                    const config = args[0] || {};
                    config.iceServers = [];
                    return new origRTC(config);
                };
                window.RTCPeerConnection.prototype = origRTC.prototype;
                if (window.webkitRTCPeerConnection) {
                    window.webkitRTCPeerConnection = window.RTCPeerConnection;
                }
            })();
        """
        )

        # Set timezone to match proxy IP's approximate region
        # For US residential proxies, America/New_York is a safe default
        try:
            tz = _guess_timezone_for_proxy(proxy)
            if ctx.pages:
                page = ctx.pages[0]
            else:
                page = await ctx.new_page()
            cdp_session = await page.context.new_cdp_session(page)
            await cdp_session.send("Emulation.setTimezoneOverride", {"timezoneId": tz})
            logger.info("Set timezone to %s to match proxy", tz)
        except Exception:
            logger.debug("Timezone override failed", exc_info=True)

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
        # Save storage state so cookies persist for this domain
        if self._storage_state_path and self._context:
            try:
                Path(self._storage_state_path).parent.mkdir(parents=True, exist_ok=True)
                await self._context.storage_state(path=self._storage_state_path)
                logger.info("Saved storage state to %s", self._storage_state_path)
            except Exception:
                logger.debug("Failed to save storage state", exc_info=True)

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


# ---------------------------------------------------------------------------
# Storage state persistence — keyed by start_url domain
# ---------------------------------------------------------------------------
# Modal sandbox: /recordings/.storage/<domain_hash>.json  (survives across runs)
# Local:         not used (real browser fingerprint is sufficient)

_STORAGE_DIR = Path("/recordings/.storage")


def _storage_path_for_url(url: str | None) -> str | None:
    """Return a storage state file path keyed by the URL's domain.

    Returns None if no URL is provided or the storage dir doesn't exist
    (i.e. we're not running in a Modal sandbox with the volume mounted).
    """
    if not url:
        return None
    # Only use persistent storage when the recordings volume is mounted
    if not _STORAGE_DIR.parent.exists():
        return None
    from urllib.parse import urlparse

    domain = urlparse(url).hostname or "default"
    domain_hash = hashlib.sha256(domain.encode()).hexdigest()[:12]
    return str(_STORAGE_DIR / f"{domain}_{domain_hash}.json")


def _guess_timezone_for_proxy(proxy: str) -> str:
    """Guess a plausible timezone for a proxy IP.

    Uses a simple IP-to-region heuristic. For production use, an IP
    geolocation service would be more accurate.
    """
    from urllib.parse import urlparse

    host = urlparse(proxy).hostname or ""

    # Simple heuristic based on first octet of common US IP ranges
    # Most residential proxies are US-based
    try:
        first_octet = int(host.split(".")[0])
        # Very rough US timezone mapping by IP range
        # East coast ranges
        if first_octet in range(24, 76):
            return "America/New_York"
        # West coast ranges
        if first_octet in range(130, 170):
            return "America/Los_Angeles"
    except (ValueError, IndexError):
        pass

    # Default to US Eastern — most common for residential proxies
    return "America/New_York"


def _parse_proxy(proxy: str) -> dict:
    """Parse a proxy URL into Playwright's proxy dict format.

    Handles both simple (http://host:port) and authenticated
    (http://user:pass@host:port) proxy URLs.
    """
    from urllib.parse import urlparse

    parsed = urlparse(proxy)
    result: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result
