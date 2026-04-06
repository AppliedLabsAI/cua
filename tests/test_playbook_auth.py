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

    async def _goto(self, url: str, **kwargs) -> None:
        self.url = url


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
        return self._login_result


@pytest.mark.asyncio
async def test_ensure_authenticated_uses_clean_current_context_without_login():
    browser = _FakeBrowser()
    auth = _TestDashboardAuth(browser, is_logged_in=True)

    result = await auth.ensure_authenticated("https://example.com/login")

    assert result is True
    browser.page.goto.assert_awaited_once()
    assert auth.login_calls == 0


@pytest.mark.asyncio
async def test_ensure_authenticated_runs_login_when_not_authenticated():
    browser = _FakeBrowser()
    auth = _TestDashboardAuth(browser, is_logged_in=False, login_result=True)

    result = await auth.ensure_authenticated("https://example.com/login")

    assert result is True
    browser.page.goto.assert_awaited_once()
    assert auth.login_calls == 1
