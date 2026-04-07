"""Dashboard authentication for clean per-run browser sessions."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import Page

    from bridge.browser import BrowserManager

from playbooks.schema import (
    AuthSuccessCriteria,
    Playbook,
    PlaybookAuthConfig,
    PlaybookCaptureConfig,
)
from playbooks.session import capture_session_artifacts
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

    async def ensure_authenticated(
        self,
        login_or_playbook: str | Playbook | PlaybookAuthConfig = "",
    ) -> bool:
        """Navigate to the login surface and authenticate for this run.

        Args:
            login_or_playbook: URL, auth config, or playbook describing login behavior.

        Returns True if authentication succeeded.
        """
        auth = self._coerce_auth_config(login_or_playbook)
        page = self._browser.page

        # Navigate to login page if needed
        if auth.login_url and auth.login_url not in page.url:
            await page.goto(
                auth.login_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS
            )

        already_authenticated = await self._is_authenticated(page, auth)

        if auth.mode == "none":
            return already_authenticated

        if auth.mode == "manual":
            return await self._wait_for_authenticated(page, auth)

        # Execute login
        await self._login(page)
        success = await self._wait_for_authenticated(page, auth)

        if success:
            if already_authenticated:
                logger.info("Already authenticated in current browser context")
            else:
                logger.info("Login successful")
        else:
            logger.error("Login failed")

        return success

    async def capture_session_artifacts(
        self,
        playbook_or_capture: Playbook | PlaybookCaptureConfig | None,
    ) -> dict[str, str]:
        """Capture allowlisted session artifacts for later API requests."""
        if playbook_or_capture is None:
            return {}
        if isinstance(playbook_or_capture, Playbook):
            capture = playbook_or_capture.capture
        else:
            capture = playbook_or_capture
        return await capture_session_artifacts(self._browser, capture)

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

        return True

    def _coerce_auth_config(
        self,
        login_or_playbook: str | Playbook | PlaybookAuthConfig,
    ) -> PlaybookAuthConfig:
        if isinstance(login_or_playbook, Playbook):
            return login_or_playbook.auth_config
        if isinstance(login_or_playbook, PlaybookAuthConfig):
            return login_or_playbook
        return PlaybookAuthConfig(mode="form_login", login_url=login_or_playbook)

    async def _is_authenticated(
        self,
        page: Page,
        auth: PlaybookAuthConfig,
    ) -> bool:
        """Check whether the current browser context appears authenticated."""
        if auth.success:
            return await self._matches_success_criteria(page, auth.success)
        return await self._matches_default_logged_in_state(page)

    async def _matches_default_logged_in_state(self, page: Page) -> bool:
        """Heuristic fallback when no explicit auth success criteria are provided."""
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

    async def _matches_success_criteria(
        self,
        page: Page,
        success: AuthSuccessCriteria,
    ) -> bool:
        if success.url_contains and success.url_contains not in page.url:
            return False

        if success.cookie_present:
            cookies = await self._browser.context.cookies()
            if not any(item.get("name") == success.cookie_present for item in cookies):
                return False

        if success.element_visible:
            try:
                handle = await page.wait_for_selector(
                    success.element_visible,
                    state="visible",
                    timeout=SELECTOR_PROBE_TIMEOUT_MS,
                )
                if not handle:
                    return False
            except Exception:
                return False

        if success.text_on_page:
            body = await page.text_content("body")
            if not body or success.text_on_page not in body:
                return False

        return True

    async def _wait_for_authenticated(
        self,
        page: Page,
        auth: PlaybookAuthConfig,
    ) -> bool:
        timeout_ms = auth.success.timeout_ms if auth.success else LOGIN_TIMEOUT_MS
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            if await self._is_authenticated(page, auth):
                return True
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=500)
            await page.wait_for_timeout(250)
        return await self._is_authenticated(page, auth)

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
