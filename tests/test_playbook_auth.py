"""Tests for playbook dashboard authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from playbooks.auth import DashboardAuth
from playbooks.schema import (
    AuthSuccessCriteria,
    CookieCapture,
    Playbook,
    PlaybookAuthConfig,
    PlaybookCaptureConfig,
    StorageCapture,
)

if TYPE_CHECKING:
    from patchright.async_api import Page


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_load_state = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.keyboard = AsyncMock()
        self._visible_selectors: set[str] = set()
        self._body_text = ""
        self._storage = {"local": {}, "session": {}}

    async def _goto(self, url: str, **kwargs) -> None:
        self.url = url

    async def wait_for_selector(self, selector: str, state="visible", timeout=0):
        if selector in self._visible_selectors:
            return object()
        raise RuntimeError(f"Selector not visible: {selector}")

    async def text_content(self, selector: str) -> str:
        assert selector == "body"
        return self._body_text

    async def evaluate(self, script: str, args):
        scope, key = args
        return self._storage[scope].get(key)


class _FakeBrowser:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.context = AsyncMock()


class _TestDashboardAuth(DashboardAuth):
    def __init__(
        self,
        browser: _FakeBrowser,
        *,
        is_logged_in: bool,
        login_result: bool = True,
    ) -> None:
        super().__init__(browser, credentials={})
        self._is_logged_in_result = is_logged_in
        self._login_result = login_result
        self.login_calls = 0

    async def _is_logged_in(self, page: Page) -> bool:
        return self._is_logged_in_result

    async def _login(self, page: Page) -> bool:
        self.login_calls += 1
        if self._login_result:
            self._is_logged_in_result = True
        return self._login_result


@pytest.mark.asyncio
async def test_ensure_authenticated_rechecks_existing_session_after_login_attempt():
    browser = _FakeBrowser()
    auth = _TestDashboardAuth(browser, is_logged_in=True, login_result=False)

    result = await auth.ensure_authenticated("https://example.com/login")

    assert result is True
    browser.page.goto.assert_awaited_once()
    assert auth.login_calls == 1


@pytest.mark.asyncio
async def test_ensure_authenticated_runs_login_when_not_authenticated():
    browser = _FakeBrowser()
    auth = _TestDashboardAuth(browser, is_logged_in=False, login_result=True)

    result = await auth.ensure_authenticated("https://example.com/login")

    assert result is True
    browser.page.goto.assert_awaited_once()
    assert auth.login_calls == 1


@pytest.mark.asyncio
async def test_manual_auth_waits_for_success_criteria_cookie():
    browser = _FakeBrowser()
    browser.context.cookies.return_value = [{"name": "sm_session_info", "value": "ok"}]
    auth = DashboardAuth(browser, credentials={})

    result = await auth.ensure_authenticated(
        PlaybookAuthConfig(
            mode="manual",
            login_url="https://example.com/login",
            success=AuthSuccessCriteria(cookie_present="sm_session_info"),
        )
    )

    assert result is True
    browser.page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_session_artifacts_reads_cookies_storage_and_static_headers():
    browser = _FakeBrowser()
    browser.context.cookies.return_value = [
        {"name": "sm_session_info", "value": "cookie-value", "domain": ".example.com"}
    ]
    browser.page._storage["local"]["user-token"] = "storage-value"
    auth = DashboardAuth(browser, credentials={})

    artifacts = await auth.capture_session_artifacts(
        Playbook(
            id="capture",
            name="Capture",
            auth_required=False,
            capture=PlaybookCaptureConfig(
                cookies=[
                    CookieCapture(
                        name="sm_session_info",
                        store_as="session_cookie",
                        domain="example.com",
                    )
                ],
                storage=[
                    StorageCapture(
                        key="user-token",
                        store_as="user_token",
                    )
                ],
                static_headers={"FFF-Auth": "V1.1"},
            ),
        )
    )

    assert artifacts == {
        "FFF-Auth": "V1.1",
        "session_cookie": "cookie-value",
        "user_token": "storage-value",
    }


@pytest.mark.asyncio
async def test_capture_session_artifacts_raises_when_required_cookie_missing():
    browser = _FakeBrowser()
    browser.context.cookies.return_value = []
    auth = DashboardAuth(browser, credentials={})

    with pytest.raises(RuntimeError, match="Required cookie 'missing' not found"):
        await auth.capture_session_artifacts(
            PlaybookCaptureConfig(
                cookies=[CookieCapture(name="missing", store_as="missing_cookie")]
            )
        )
