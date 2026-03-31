"""Tests for the shared primitive page action executor."""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.page_actions import PageActionConfig, _extract_markdown, execute_page_action
from credentials import SecretValue


class _FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[tuple[str, int]] = []
        self.pressed: list[str] = []

    async def type(self, text: str, delay: int = 0) -> None:
        self.typed.append((text, delay))

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class _FakeMouse:
    def __init__(self) -> None:
        self.wheels: list[tuple[int, int]] = []

    async def wheel(self, delta_x: int, delta_y: int) -> None:
        self.wheels.append((delta_x, delta_y))


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com/start"
        self.keyboard = _FakeKeyboard()
        self.mouse = _FakeMouse()
        self.filled: list[tuple[str, str, int]] = []
        self.selected: list[tuple[str, str, int]] = []
        self.evaluated: list[tuple[str, object]] = []

    async def goto(self, url: str, **kwargs):
        self.url = url
        return type("Resp", (), {"status": 200})()

    async def click(self, selector: str, **kwargs) -> None:
        self.last_click = selector

    async def fill(self, selector: str, value: str, timeout: int) -> None:
        self.filled.append((selector, value, timeout))

    async def wait_for_selector(self, selector: str, **kwargs):
        return object()

    async def wait_for_load_state(self, *args, **kwargs) -> None:
        return None

    async def select_option(self, selector: str, value: str, timeout: int) -> None:
        self.selected.append((selector, value, timeout))

    async def evaluate(self, script: str, arg=None):
        self.evaluated.append((script, arg))
        if "window.location.href" in script:
            self.url = "https://example.com/next"
        if isinstance(arg, list) and arg and arg[0] == "#shop":
            return "f1a1523a-a020-4417-a6fb-85f00ce929af"
        return "ok"

    async def inner_text(self, selector: str, timeout: int) -> str:
        return f"text:{selector}:{timeout}"

    async def inner_html(self, selector: str, timeout: int) -> str:
        return f"<div>{selector}</div>"


def _config() -> PageActionConfig:
    return PageActionConfig(
        action_timeout_ms=3000,
        navigation_timeout_ms=7000,
        scroll_unit=200,
        type_delay_ms=0,
        settle_after_click=True,
        settle_after_evaluate=True,
        settle_timeout_ms=1000,
        settle_sleep_s=0,
    )


def test_key_press_with_selector_uses_fill():
    page = _FakePage()
    outcome = asyncio.run(
        execute_page_action(
            page,
            "key_press",
            {"selector": "#email", "text": "user@example.com"},
            config=_config(),
        )
    )

    assert page.filled == [("#email", "user@example.com", 3000)]
    assert outcome.text == "Typed 'user@example.com'"


def test_key_press_with_credential_ref_uses_fill_without_exposing_value():
    page = _FakePage()
    outcome = asyncio.run(
        execute_page_action(
            page,
            "key_press",
            {"selector": "#password", "credential_ref": "password"},
            config=_config(),
            credentials={"password": SecretValue("s3cr3t")},
        )
    )

    assert page.filled == [("#password", "s3cr3t", 3000)]
    assert outcome.text == "Typed credential 'password'"


def test_key_press_with_unknown_credential_ref_fails():
    page = _FakePage()

    with pytest.raises(KeyError):
        asyncio.run(
            execute_page_action(
                page,
                "key_press",
                {"selector": "#password", "credential_ref": "password"},
                config=_config(),
                credentials={"username": SecretValue("alice")},
            )
        )


def test_key_press_rejects_text_and_credential_ref_together():
    page = _FakePage()

    with pytest.raises(ValueError, match="either.*credential_ref"):
        asyncio.run(
            execute_page_action(
                page,
                "key_press",
                {
                    "selector": "#password",
                    "text": "plain",
                    "credential_ref": "password",
                },
                config=_config(),
                credentials={"password": SecretValue("s3cr3t")},
            )
        )


def test_select_action_uses_select_option():
    page = _FakePage()
    outcome = asyncio.run(
        execute_page_action(
            page,
            "select",
            {"selector": "#country", "value": "US"},
            config=_config(),
        )
    )

    assert page.selected == [("#country", "US", 3000)]
    assert outcome.text == "Selected option"


def test_evaluate_reports_page_change():
    page = _FakePage()
    outcome = asyncio.run(
        execute_page_action(
            page,
            "evaluate",
            {"script": "window.location.href = '/next';"},
            config=_config(),
        )
    )

    assert outcome.page_changed is True


def test_extract_value_returns_raw_field_value():
    page = _FakePage()
    outcome = asyncio.run(
        execute_page_action(
            page,
            "extract",
            {"selector": "#shop", "mode": "value"},
            config=_config(),
        )
    )

    assert outcome.text == "f1a1523a-a020-4417-a6fb-85f00ce929af"


# ---------------------------------------------------------------------------
# _extract_markdown tests
# ---------------------------------------------------------------------------


def test_extract_markdown_success():
    """Readability returns valid JSON → html_to_markdown + truncate_markdown."""
    page = AsyncMock()
    raw = json.dumps(
        {"html": "<h1>Title</h1><p>Body</p>", "url": "https://example.com"}
    )
    page.evaluate = AsyncMock(return_value=raw)

    mock_h2m = MagicMock(return_value="# Title\n\nBody")
    mock_trunc = MagicMock(return_value="# Title\n\nBody")
    fake_markdown = MagicMock()
    fake_markdown.html_to_markdown = mock_h2m
    fake_markdown.truncate_markdown = mock_trunc
    with patch.dict(sys.modules, {"bridge.markdown": fake_markdown}):
        result = asyncio.run(_extract_markdown(page))

    mock_h2m.assert_called_once_with(
        "<h1>Title</h1><p>Body</p>", base_url="https://example.com"
    )
    mock_trunc.assert_called_once_with("# Title\n\nBody")
    assert result == "# Title\n\nBody"


def test_extract_markdown_null_falls_back_to_inner_text():
    """Readability returns null → falls back to page.inner_text('body')."""
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    page.inner_text = AsyncMock(return_value="Plain text fallback")

    result = asyncio.run(_extract_markdown(page))

    page.inner_text.assert_awaited_once_with("body")
    assert result == "Plain text fallback"


def test_extract_markdown_invalid_json_falls_back_to_inner_text():
    """Readability returns invalid JSON → falls back to page.inner_text('body')."""
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value="not valid json{{{")
    page.inner_text = AsyncMock(return_value="Fallback text")

    result = asyncio.run(_extract_markdown(page))

    page.inner_text.assert_awaited_once_with("body")
    assert result == "Fallback text"
