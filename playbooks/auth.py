"""Dashboard authentication — login flow with session persistence."""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from patchright.async_api import Page

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext

    from bridge.browser import BrowserManager

log = logging.getLogger(__name__)

_SESSION_DIR = Path.home() / ".cua" / "sessions"
_LOGIN_TIMEOUT = 15_000


class DashboardAuth:
    """Handle dashboard login with session persistence.

    Uses Playwright's storage_state to save/restore cookies and localStorage,
    avoiding re-login on every run.
    """

    def __init__(
        self,
        browser: BrowserManager,
        credentials: dict,
        session_id: str = "default",
    ) -> None:
        self._browser = browser
        self._credentials = credentials
        self._session_id = session_id
        self._session_path = _SESSION_DIR / f"{session_id}.json"

    async def ensure_authenticated(self, login_url: str = "") -> bool:
        """Check if already logged in; if not, execute login flow.

        Args:
            login_url: URL of the login page. If empty, uses current page.

        Returns True if authentication succeeded.
        """
        page = self._browser.page

        # Try restoring a saved session first
        if await self._restore_session(self._browser.context):
            # Reload the page to apply restored cookies
            if login_url:
                await page.goto(
                    login_url, wait_until="domcontentloaded", timeout=_LOGIN_TIMEOUT
                )
            else:
                await page.reload(wait_until="domcontentloaded", timeout=_LOGIN_TIMEOUT)

            if await self._is_logged_in(page):
                log.info("Session restored successfully")
                return True
            else:
                log.info("Restored session expired, logging in fresh")

        # Navigate to login page if needed
        if login_url and login_url not in page.url:
            await page.goto(
                login_url, wait_until="domcontentloaded", timeout=_LOGIN_TIMEOUT
            )

        # Execute login
        success = await self._login(page)

        if success:
            await self._save_session(self._browser.context)
            log.info("Login successful, session saved")
        else:
            log.error("Login failed")

        return success

    async def _login(self, page: Page) -> bool:
        """Execute the login flow using provided credentials.

        Looks for common login form patterns and fills them.
        """
        username = self._credentials.get("username", self._credentials.get("email", ""))
        password = self._credentials.get("password", "")

        if not username or not password:
            log.error("Missing username/email or password in credentials")
            return False

        # Try common username/email selectors
        username_selectors = [
            "input[type='email']",
            "input[name='email']",
            "input[name='username']",
            "input[id='email']",
            "input[id='username']",
            "input[placeholder*='email' i]",
            "input[placeholder*='username' i]",
        ]

        username_filled = False
        for selector in username_selectors:
            try:
                handle = await page.wait_for_selector(
                    selector, state="visible", timeout=500
                )
                if handle:
                    await page.fill(selector, username, timeout=3000)
                    username_filled = True
                    break
            except Exception:
                continue

        if not username_filled:
            log.error("Could not find username/email input field")
            return False

        # Fill password
        password_selectors = [
            "input[type='password']",
            "input[name='password']",
            "input[id='password']",
        ]

        password_filled = False
        for selector in password_selectors:
            try:
                handle = await page.wait_for_selector(
                    selector, state="visible", timeout=500
                )
                if handle:
                    await page.fill(selector, password, timeout=3000)
                    password_filled = True
                    break
            except Exception:
                continue

        if not password_filled:
            log.error("Could not find password input field")
            return False

        # Submit the form
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "text=Log in",
            "text=Sign in",
            "text=Login",
            "text=Submit",
            "role=button[name='Log in' i]",
            "role=button[name='Sign in' i]",
        ]

        for selector in submit_selectors:
            try:
                handle = await page.wait_for_selector(
                    selector, state="visible", timeout=500
                )
                if handle:
                    await page.click(selector, timeout=3000)
                    break
            except Exception:
                continue
        else:
            # Fallback: press Enter
            await page.keyboard.press("Enter")

        # Wait for navigation after login
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=_LOGIN_TIMEOUT)

        return await self._is_logged_in(page)

    async def _is_logged_in(self, page: Page) -> bool:
        """Check if the current page indicates a logged-in state.

        Heuristic: absence of login form elements suggests logged in.
        """
        # If there's still a visible password field, probably not logged in
        try:
            handle = await page.wait_for_selector(
                "input[type='password']", state="visible", timeout=1500
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

    async def _save_session(self, context: BrowserContext) -> None:
        """Persist cookies and localStorage for session reuse."""
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            state = await context.storage_state()
            with open(self._session_path, "w") as f:
                json.dump(state, f)
            log.info("Session state saved: %s", self._session_path)
        except Exception as exc:
            log.warning("Failed to save session state: %s", exc)

    async def _restore_session(self, context: BrowserContext) -> bool:
        """Restore a previously saved session."""
        if not self._session_path.exists():
            return False

        try:
            with open(self._session_path) as f:
                state = json.load(f)

            # Add cookies from saved state
            cookies = state.get("cookies", [])
            if cookies:
                await context.add_cookies(cookies)
                log.info("Restored %d cookies from saved session", len(cookies))
                return True
        except Exception as exc:
            log.warning("Failed to restore session state: %s", exc)

        return False
