"""Tests for bridge.execution orchestration helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bridge import ActionResult
from bridge.execution import SequenceExecutor, execute_dom_action


def test_execute_dom_action_rejects_unknown_action():
    result = asyncio.run(
        execute_dom_action(
            "does_not_exist",
            {},
            SimpleNamespace(page=object()),
        )
    )

    assert result.error == "Unknown browser_dom action: does_not_exist"


def test_sequence_executor_combines_step_results_and_page_context(monkeypatch):
    browser = SimpleNamespace(page=object())

    async def _fake_execute_dom_action(
        action,
        step,
        browser_obj,
        *,
        include_page_context=True,
        filter_config=None,
        credentials=None,
    ):
        return ActionResult(text=f"{action} ok")

    async def _fake_attach_page_context(browser_obj, filter_config):
        return "\n\nCTX"

    monkeypatch.setattr("bridge.execution.execute_dom_action", _fake_execute_dom_action)
    monkeypatch.setattr(
        "bridge.execution.attach_page_context", _fake_attach_page_context
    )

    result = asyncio.run(
        SequenceExecutor(browser=browser).run(
            {"steps": [{"action": "click"}, {"action": "scroll"}]}
        )
    )

    assert result.error is None
    assert "Step 1 (click)" in (result.text or "")
    assert "Step 2 (scroll)" in (result.text or "")
    assert (result.text or "").endswith("CTX")


def test_sequence_executor_returns_screenshot_on_step_failure(monkeypatch):
    browser = SimpleNamespace(page=object())
    calls: list[str] = []

    async def _fake_execute_dom_action(
        action,
        step,
        browser_obj,
        *,
        include_page_context=True,
        filter_config=None,
        credentials=None,
    ):
        calls.append(action)
        if len(calls) == 1:
            return ActionResult(text="first ok")
        return ActionResult(error="second failed")

    async def _fake_page_screenshot(page):
        return "fake-b64"

    monkeypatch.setattr("bridge.execution.execute_dom_action", _fake_execute_dom_action)
    monkeypatch.setattr("bridge.execution.page_screenshot", _fake_page_screenshot)

    result = asyncio.run(
        SequenceExecutor(browser=browser).run(
            {"steps": [{"action": "click"}, {"action": "scroll"}]}
        )
    )

    assert result.screenshot_b64 == "fake-b64"
    assert result.error == "Step 2 (scroll): second failed"
    assert "Step 1 (click)" in (result.text or "")
    assert "first ok" in (result.text or "")
