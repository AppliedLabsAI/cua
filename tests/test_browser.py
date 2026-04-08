"""Tests for browser launch helpers and storage-state persistence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from bridge import browser


@pytest.mark.asyncio
async def test_lookup_timezone_for_proxy_prefers_geolocated_value(monkeypatch):
    async def _fake_fetch(host: str) -> str | None:
        assert host == "9.142.46.117"
        return "America/Los_Angeles"

    monkeypatch.setattr(browser, "_fetch_timezone_for_host", _fake_fetch)

    timezone = await browser._lookup_timezone_for_proxy(
        "http://user:pass@9.142.46.117:6786/"
    )

    assert timezone == "America/Los_Angeles"


def test_restore_storage_state_requires_success_marker_and_allows_alias(
    monkeypatch, tmp_path
):
    storage_dir = tmp_path / ".storage"
    monkeypatch.setattr(browser, "_STORAGE_DIR", storage_dir)

    alias_path = browser._storage_path_for_domain("fabfitfun.com")
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text("{}")

    url = "https://cs-admin.fabfitfun.com"
    assert browser._restore_storage_state_path_for_url(url) is None

    browser._storage_success_marker(alias_path).write_text("ok")

    assert browser._restore_storage_state_path_for_url(url) == str(alias_path)


class _FakeLocator:
    def __init__(self, visible: bool) -> None:
        self.first = self
        self._visible = visible

    async def is_visible(self, timeout: int = 0) -> bool:
        return self._visible


class _FakePage:
    def __init__(self, url: str, visible_selectors: set[str] | None = None) -> None:
        self.url = url
        self._visible_selectors = visible_selectors or set()

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector in self._visible_selectors)


@pytest.mark.asyncio
async def test_page_looks_unauthenticated_for_hcaptcha_login_page():
    page = _FakePage(
        "https://login.fabfitfun.com",
        visible_selectors={'iframe[title="hCaptcha challenge"]'},
    )

    assert await browser._page_looks_authenticated(page) is False


@pytest.mark.asyncio
async def test_close_skips_storage_persist_when_page_still_looks_like_login(tmp_path):
    manager = browser.BrowserManager()
    manager._storage_state_paths = [str(tmp_path / "state.json")]
    manager._context = cast(
        Any,
        SimpleNamespace(storage_state=AsyncMock(return_value={})),
    )
    manager._page = cast(Any, _FakePage("https://login.fabfitfun.com/signin"))
    manager._browser = cast(Any, SimpleNamespace(close=AsyncMock()))
    manager._playwright = None

    await manager.close(save_storage_state=True)

    manager._context = None
    assert not Path(tmp_path / "state.json").exists()


def test_invalidate_storage_state_paths_removes_success_markers(tmp_path):
    manager = browser.BrowserManager()
    storage_path = tmp_path / "state.json"
    ok_path = Path(f"{storage_path}.ok")
    storage_path.write_text("{}")
    ok_path.write_text("ok")
    manager._storage_state_paths = [str(storage_path)]

    manager._invalidate_storage_state_paths()

    assert storage_path.exists()
    assert not ok_path.exists()
