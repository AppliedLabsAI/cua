"""Tests for playbook dashboard authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from playbooks.auth import DashboardAuth

if TYPE_CHECKING:
    from patchright.async_api import Page


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_selector = AsyncMock(return_value=object())
        self.click = AsyncMock()
        self.keyboard = _FakeKeyboard()

    async def _goto(self, url: str, **kwargs) -> None:
        self.url = url


class _FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []
        self.typed: list[tuple[str, int]] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def type(self, text: str, delay: int = 0) -> None:
        self.typed.append((text, delay))


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
async def test_fill_first_visible_types_like_user():
    browser = _FakeBrowser()
    auth = DashboardAuth(
        browser,
        credentials={"email": "user@example.com", "password": "secret"},
    )

    result = await auth._fill_first_visible(
        browser.page,
        ["input[type='email']"],
        "user@example.com",
    )

    assert result is True
    browser.page.wait_for_selector.assert_awaited_once()
    browser.page.click.assert_awaited_once()
    assert browser.page.keyboard.pressed == ["Control+A", "Backspace"]
    assert browser.page.keyboard.typed == [("user@example.com", 50)]
