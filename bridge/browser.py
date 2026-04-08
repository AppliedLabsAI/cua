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
import json
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx
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
from bridge.stealth import get_stealth_launch_args, inject_stealth_scripts
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
        self._storage_state_paths: list[str] = []

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
            proxy_timezone = await _lookup_timezone_for_proxy(proxy)
            context_kwargs["timezone_id"] = proxy_timezone
        else:
            proxy_timezone = None

        # Restore cookies/localStorage keyed by start_url domain so
        # hCaptcha passes and auth sessions persist across runs.
        self._storage_state_paths = _storage_paths_for_url(start_url)
        storage_state_path = _restore_storage_state_path_for_url(start_url)
        if storage_state_path:
            context_kwargs["storage_state"] = storage_state_path
            logger.info("Restored storage state from %s", storage_state_path)

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)

        await inject_stealth_scripts(self._context)

        # When using a proxy, block WebRTC from leaking the real IP.
        if proxy:
            await self._apply_proxy_protections()
            logger.info("Set timezone to %s to match proxy", proxy_timezone)

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
            if storage_state_path and not await _page_looks_authenticated(self._page):
                logger.info(
                    "Restored storage state from %s appears unauthenticated; "
                    "disabling saved-session reuse",
                    storage_state_path,
                )
                self._invalidate_storage_state_paths()

    async def _apply_proxy_protections(self) -> None:
        """Prevent WebRTC IP leaks when using a proxy.

        hCaptcha cross-checks:
        1. WebRTC STUN requests (reveals real IP behind proxy)

        We block WebRTC at both the current page and init-script levels.
        """
        assert self._context is not None  # Called right after context creation
        ctx = self._context

        # Block WebRTC IP leak via CDP
        try:
            if ctx.pages:
                await ctx.pages[0].evaluate("""
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

    async def close(self, *, save_storage_state: bool = True) -> None:
        """Shut down browser and Patchright.

        Tolerates already-dead connections (e.g. after Ctrl+C).
        """
        # Persist only known-good storage states so failed auth flows do not
        # overwrite the last working session.
        if save_storage_state and self._storage_state_paths and self._context:
            if self._page and not await _page_looks_authenticated(self._page):
                logger.info(
                    "Skipping storage state save because the current page still "
                    "looks like a login or hCaptcha challenge"
                )
                self._invalidate_storage_state_paths()
            else:
                try:
                    storage_state_json = json.dumps(await self._context.storage_state())
                    saved_paths: list[str] = []
                    for storage_state_path in self._storage_state_paths:
                        path = Path(storage_state_path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(storage_state_json)
                        _storage_success_marker(path).write_text("ok")
                        saved_paths.append(storage_state_path)
                    if saved_paths:
                        logger.info("Saved storage state to %s", ", ".join(saved_paths))
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

    def _invalidate_storage_state_paths(self) -> None:
        """Disable saved-session reuse for the current storage-state files."""
        for storage_state_path in self._storage_state_paths:
            with contextlib.suppress(FileNotFoundError):
                _storage_success_marker(storage_state_path).unlink()


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
    paths = _storage_paths_for_url(url)
    if not paths:
        return None
    return paths[0]


def _storage_paths_for_url(url: str | None) -> list[str]:
    """Return candidate storage-state paths for a URL.

    The exact hostname remains the primary key. We also write an alias for the
    registrable domain so related subdomains can reuse the same good session.
    """
    if not url:
        return []
    if not _STORAGE_DIR.parent.exists():
        return []
    from urllib.parse import urlparse

    domain = urlparse(url).hostname or "default"
    domains = [domain]
    registrable_domain = _registrable_domain(domain)
    if registrable_domain and registrable_domain != domain:
        domains.append(registrable_domain)

    unique_domains = list(dict.fromkeys(domains))
    return [str(_storage_path_for_domain(value)) for value in unique_domains]


def _restore_storage_state_path_for_url(url: str | None) -> str | None:
    """Return the first known-good storage state path for the URL."""
    for storage_state_path in _storage_paths_for_url(url):
        path = Path(storage_state_path)
        if path.exists() and _storage_success_marker(path).exists():
            return storage_state_path
    return None


def _storage_path_for_domain(domain: str) -> Path:
    domain_hash = hashlib.sha256(domain.encode()).hexdigest()[:12]
    return _STORAGE_DIR / f"{domain}_{domain_hash}.json"


def _storage_success_marker(path: str | Path) -> Path:
    return Path(f"{path}.ok")


def _registrable_domain(domain: str) -> str:
    """Return a simple registrable-domain alias for subdomain reuse."""
    try:
        parts = domain.split(".")
        if len(parts) < 2 or all(part.isdigit() for part in parts):
            return domain
        return ".".join(parts[-2:])
    except Exception:
        return domain


async def _lookup_timezone_for_proxy(proxy: str) -> str:
    """Resolve the proxy timezone via IP geolocation, with heuristic fallback."""
    from urllib.parse import urlparse

    host = urlparse(proxy).hostname or ""
    timezone = await _fetch_timezone_for_host(host)
    return timezone or _guess_timezone_for_proxy(proxy)


async def _fetch_timezone_for_host(host: str) -> str | None:
    """Fetch timezone data for a proxy host from ipinfo."""
    if not host:
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=3.0,
            trust_env=False,
        ) as client:
            response = await client.get(f"https://ipinfo.io/{host}/json")
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.debug("Proxy geolocation lookup failed for %s", host, exc_info=True)
        return None

    timezone = data.get("timezone")
    if isinstance(timezone, str) and timezone:
        return timezone
    return None


async def _page_looks_authenticated(page: Any) -> bool:
    """Return False when the page still looks like login/captcha state."""
    url_lower = str(getattr(page, "url", "")).lower()
    if any(marker in url_lower for marker in ("/login", "/signin", "/sign-in")):
        return False
    if "login." in url_lower:
        return False

    suspicious_selectors = (
        "input[type='password']",
        'iframe[title="hCaptcha challenge"]',
        'iframe[data-hcaptcha-widget-id][aria-hidden="true"]',
    )
    for selector in suspicious_selectors:
        if await _selector_is_visible(page, selector):
            return False
    return True


async def _selector_is_visible(page: Any, selector: str, timeout_ms: int = 300) -> bool:
    """Best-effort visibility probe used by auth/session heuristics."""
    try:
        locator = page.locator(selector).first
        return await locator.is_visible(timeout=timeout_ms)
    except Exception:
        return False


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
