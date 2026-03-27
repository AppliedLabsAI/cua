"""Dashboard authentication — login flow with session persistence."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page

    from bridge.browser import BrowserManager

from settings import (
    ACTION_TIMEOUT_MS,
    LOGIN_DETECT_TIMEOUT_MS,
    LOGIN_TIMEOUT_MS,
    SELECTOR_PROBE_TIMEOUT_MS,
)

log = logging.getLogger(__name__)

_SESSION_DIR = Path.home() / ".cua" / "sessions"
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


class DashboardSessionStore:
    """Persist session state in per-origin files to avoid cross-site reuse."""

    def __init__(self, base_dir: Path = _SESSION_DIR) -> None:
        self._base_dir = base_dir

    def path_for(self, session_id: str, origin_hint: str) -> Path:
        parsed = urlparse(origin_hint)
        host = parsed.netloc or parsed.path or "default"
        safe_host = "".join(ch if ch.isalnum() else "_" for ch in host).strip("_")
        namespace = safe_host or "default"
        return self._base_dir / namespace / f"{session_id}.json"


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
        session_store: DashboardSessionStore | None = None,
    ) -> None:
        self._browser = browser
        self._credentials = credentials
        self._session_id = session_id
        self._session_store = session_store or DashboardSessionStore()

    async def ensure_authenticated(self, login_url: str = "") -> bool:
        """Check if already logged in; if not, execute login flow.

        Args:
            login_url: URL of the login page. If empty, uses current page.

        Returns True if authentication succeeded.
        """
        page = self._browser.page
        session_path = self._session_store.path_for(
            self._session_id,
            login_url or page.url,
        )

        # Try restoring a saved session first
        if await self._restore_session(self._browser.context, session_path):
            # Reload the page to apply restored cookies
            if login_url:
                await page.goto(
                    login_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS
                )
            else:
                await page.reload(
                    wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS
                )

            if await self._is_logged_in(page):
                log.info("Session restored successfully")
                return True
            else:
                log.info("Restored session expired, logging in fresh")

        # Navigate to login page if needed
        if login_url and login_url not in page.url:
            await page.goto(
                login_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS
            )

        # Execute login
        success = await self._login(page)

        if success:
            await self._save_session(self._browser.context, session_path)
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

        if not await self._fill_first_visible(page, _USERNAME_SELECTORS, username):
            log.error("Could not find username/email input field")
            return False

        if not await self._fill_first_visible(page, _PASSWORD_SELECTORS, password):
            log.error("Could not find password input field")
            return False

        if not await self._click_first_visible(page, _SUBMIT_SELECTORS):
            # Fallback: press Enter
            await page.keyboard.press("Enter")

        # Wait for navigation after login
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

    async def _save_session(self, context: BrowserContext, session_path: Path) -> None:
        """Persist cookies and localStorage for session reuse."""
        try:
            session_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(session_path.parent, 0o700)
            state = await context.storage_state()
            fd = os.open(
                str(session_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            log.info("Session state saved: %s", session_path)
        except Exception as exc:
            log.warning("Failed to save session state: %s", exc)

    async def _restore_session(
        self,
        context: BrowserContext,
        session_path: Path,
    ) -> bool:
        """Restore a previously saved session (cookies + per-origin localStorage)."""
        if not session_path.exists():
            return False

        try:
            with open(session_path) as f:
                state = json.load(f)

            # Restore cookies
            cookies = state.get("cookies", [])
            if cookies:
                await context.add_cookies(cookies)
                log.info("Restored %d cookies from saved session", len(cookies))

            # Restore per-origin localStorage
            origins = state.get("origins", [])
            if origins:
                page = self._browser.page
                for origin_data in origins:
                    origin_url = origin_data.get("origin", "")
                    ls_entries = origin_data.get("localStorage", [])
                    if not origin_url or not ls_entries:
                        continue
                    try:
                        await page.goto(
                            origin_url,
                            wait_until="domcontentloaded",
                            timeout=LOGIN_TIMEOUT_MS,
                        )
                        await page.evaluate(
                            """(entries) => {
                                for (const e of entries) {
                                    localStorage.setItem(e.name, e.value);
                                }
                            }""",
                            ls_entries,
                        )
                        log.info(
                            "Restored %d localStorage entries for %s",
                            len(ls_entries),
                            origin_url,
                        )
                    except Exception as exc:
                        log.debug(
                            "Failed to restore localStorage for %s: %s",
                            origin_url,
                            exc,
                        )

            return bool(cookies or origins)
        except Exception as exc:
            log.warning("Failed to restore session state: %s", exc)

        return False

    async def _fill_first_visible(
        self,
        page: Page,
        selectors: list[str],
        value: str,
    ) -> bool:
        for selector in selectors:
            try:
                handle = await page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=SELECTOR_PROBE_TIMEOUT_MS,
                )
                if handle:
                    await page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        return False

    async def _click_first_visible(self, page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                handle = await page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=SELECTOR_PROBE_TIMEOUT_MS,
                )
                if handle:
                    await page.click(selector, timeout=ACTION_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        return False
