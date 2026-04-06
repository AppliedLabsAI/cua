"""Dashboard authentication for clean per-run browser sessions."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import Page

    from bridge.browser import BrowserManager

from settings import (
    ACTION_TIMEOUT_MS,
    LOGIN_DETECT_TIMEOUT_MS,
    LOGIN_TIMEOUT_MS,
    SELECTOR_PROBE_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

_USERNAME_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input[id='email']",
    "input[id='username']",
    "input[placeholder*='email' i]",
    "input[placeholder*='username' i]",
]
_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
    "input[id='password']",
]
_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "text=Log in",
    "text=Sign in",
    "text=Login",
    "text=Submit",
    "role=button[name='Log in' i]",
    "role=button[name='Sign in' i]",
]


class DashboardAuth:
    """Handle dashboard login for a fresh browser context on each run."""

    def __init__(
        self,
        browser: BrowserManager,
        credentials: dict,
    ) -> None:
        self._browser = browser
        self._credentials = credentials

    async def ensure_authenticated(self, login_url: str = "") -> bool:
        """Navigate to the login surface and authenticate for this run.

        Args:
            login_url: URL of the login page. If empty, uses current page.

        Returns True if authentication succeeded.
        """
        page = self._browser.page

        # Navigate to login page if needed
        if login_url and login_url not in page.url:
            await page.goto(
                login_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS
            )

        if await self._is_logged_in(page):
            logger.info("Already authenticated in current browser context")
            return True

        # Execute login
        success = await self._login(page)

        if success:
            logger.info("Login successful")
        else:
            logger.error("Login failed")

        return success

    async def _login(self, page: Page) -> bool:
        """Execute the login flow using provided credentials.

        Looks for common login form patterns and fills them.
        """
        from credentials import resolve_credential_ref

        username = ""
        password = ""
        with contextlib.suppress(KeyError):
            username = resolve_credential_ref(self._credentials, "username")
        if not username:
            with contextlib.suppress(KeyError):
                username = resolve_credential_ref(self._credentials, "email")
        with contextlib.suppress(KeyError):
            password = resolve_credential_ref(self._credentials, "password")

        if not username or not password:
            logger.error("Missing username/email or password in credentials")
            return False

        # Wait for the login page to render (JS frameworks need time after
        # cross-domain redirects). Once any input is visible, probe quickly.
        with contextlib.suppress(Exception):
            await page.wait_for_selector(
                "input", state="visible", timeout=LOGIN_TIMEOUT_MS
            )

        if not await self._fill_first_visible(page, _USERNAME_SELECTORS, username):
            logger.error("Could not find username/email input field")
            return False

        if not await self._fill_first_visible(page, _PASSWORD_SELECTORS, password):
            logger.error("Could not find password input field")
            return False

        if not await self._click_first_visible(page, _SUBMIT_SELECTORS):
            # Fallback: press Enter
            await page.keyboard.press("Enter")

        # Wait for navigation after login — the page may redirect cross-domain
        # (e.g., login.example.com → app.example.com), so wait for a URL change.
        with contextlib.suppress(Exception):
            await page.wait_for_url(
                lambda url: (
                    "/login" not in url.lower()
                    and "/signin" not in url.lower()
                    and "/sign-in" not in url.lower()
                ),
                timeout=LOGIN_TIMEOUT_MS,
            )
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=LOGIN_TIMEOUT_MS)

        return await self._is_logged_in(page)

    async def _is_logged_in(self, page: Page) -> bool:
        """Check if the current page indicates a logged-in state.

        Heuristic: absence of login form elements suggests logged in.
        """
        # If there's still a visible password field, probably not logged in
        try:
            handle = await page.wait_for_selector(
                "input[type='password']",
                state="visible",
                timeout=LOGIN_DETECT_TIMEOUT_MS,
            )
            if handle:
                return False
        except Exception:
            pass  # No password field visible → probably logged in

        # Check for common login page indicators
        url_lower = page.url.lower()
        if any(
            indicator in url_lower for indicator in ["/login", "/signin", "/sign-in"]
        ):
            # Still on login page — might have failed
            body = await page.text_content("body")
            if body and any(
                err in body.lower()
                for err in ["invalid", "incorrect", "wrong password", "try again"]
            ):
                return False

        return True

    async def _fill_first_visible(
        self,
        page: Page,
        selectors: list[str],
        value: str,
        timeout_ms: int = SELECTOR_PROBE_TIMEOUT_MS,
    ) -> bool:
        for selector in selectors:
            try:
                handle = await page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=timeout_ms,
                )
                if handle:
                    await page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        return False

    async def _click_first_visible(
        self,
        page: Page,
        selectors: list[str],
        timeout_ms: int = SELECTOR_PROBE_TIMEOUT_MS,
    ) -> bool:
        for selector in selectors:
            try:
                handle = await page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=timeout_ms,
                )
                if handle:
                    await page.click(selector, timeout=ACTION_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        return False
