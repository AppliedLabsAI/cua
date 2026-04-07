"""Tests for bridge.router orchestration behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bridge import DOM_MARKER, ActionResult
from bridge.router import ActionRouter


class _FakeBrowser:
    def __init__(self) -> None:
        self.page = SimpleNamespace(url="https://example.com")
        self.wait_calls = 0

    async def wait_for_active_page(self) -> None:
        self.wait_calls += 1


class _FakeBlinders:
    def __init__(self) -> None:
        self.scope = SimpleNamespace()

    def to_js_filter_config(self) -> dict[str, bool]:
        return {"enabled": True}

    def filter_snapshot(self, dom: str) -> str:
        return dom.replace("SECRET", "FILTERED")


class _FakeVerifier:
    def __init__(self, scope, guardrails, directive: str = "") -> None:
        self.scope = scope

    async def check(self, action, tool_input, *, page_url: str, page_title: str):
        return None

    def check_post_navigation(self, url: str):
        return None


async def _run_router_action(router: ActionRouter, tool_input: dict) -> dict:
    try:
        return await router.execute("browser_dom", tool_input)
    finally:
        await router.drain_background()


def test_router_filters_dom_and_namespaces_persisted_logs(monkeypatch):
    browser = _FakeBrowser()
    persisted: list[str] = []

    async def _fake_execute_dom_action(
        action,
        tool_input,
        browser_obj,
        *,
        include_page_context=True,
        filter_config=None,
        credentials=None,
    ):
        return ActionResult(text=f"Clicked\n{DOM_MARKER}\nSECRET")

    async def _fake_persist(log_entry, *, run_id: str = ""):
        persisted.append(run_id)
        return "/tmp/fake.json"

    async def _fake_handle_captcha(page):
        return SimpleNamespace(
            detected=False,
            message="",
            captcha_type=None,
            resolved=False,
            wait_time_ms=0,
        )

    monkeypatch.setattr("bridge.router.execute_dom_action", _fake_execute_dom_action)
    monkeypatch.setattr("bridge.router.persist_action_log", _fake_persist)
    monkeypatch.setattr("bridge.router.handle_captcha_if_present", _fake_handle_captcha)
    monkeypatch.setattr("blinders.verifier.ScopeVerifier", _FakeVerifier)

    router = ActionRouter(
        browser=browser,
        blinders=_FakeBlinders(),
        run_id="run-123",
    )

    result = asyncio.run(_run_router_action(router, {"action": "click"}))

    assert browser.wait_calls == 1
    assert persisted == ["run-123"]
    assert result["is_error"] is False
    assert result["content"][0]["text"] == f"Clicked\n{DOM_MARKER}\nFILTERED"


def test_router_prepends_captcha_message_for_navigation_actions(monkeypatch):
    browser = _FakeBrowser()

    async def _fake_execute_dom_action(
        action,
        tool_input,
        browser_obj,
        *,
        include_page_context=True,
        filter_config=None,
        credentials=None,
    ):
        return ActionResult(text="Clicked")

    async def _fake_persist(log_entry, *, run_id: str = ""):
        return "/tmp/fake.json"

    async def _fake_handle_captcha(page):
        return SimpleNamespace(
            detected=True,
            message="CAPTCHA solved",
            captcha_type="cloudflare",
            resolved=True,
            wait_time_ms=250,
        )

    monkeypatch.setattr("bridge.router.execute_dom_action", _fake_execute_dom_action)
    monkeypatch.setattr("bridge.router.persist_action_log", _fake_persist)
    monkeypatch.setattr("bridge.router.handle_captcha_if_present", _fake_handle_captcha)

    router = ActionRouter(browser=browser, run_id="run-123")
    result = asyncio.run(_run_router_action(router, {"action": "click"}))

    assert result["is_error"] is False
    assert result["content"][0]["text"] == "CAPTCHA solved\nClicked"
