"""Tests for action log persistence helpers."""

from __future__ import annotations

import json

import pytest

from actionlog.actions import ActionLog, save_action_log


def _log(step: int, action: str = "click") -> ActionLog:
    return ActionLog(
        step=step,
        timestamp="2026-01-01T00:00:00Z",
        tool="browser_dom",
        action=action,
        input_summary=f"{action} step {step}",
        tool_input={"selector": f"#btn-{step}"},
        duration_ms=100,
        success=True,
    )


@pytest.mark.asyncio
async def test_save_action_log_includes_session_memory(tmp_path):
    path = tmp_path / "action_log.json"

    await save_action_log(
        [_log(1), _log(2)],
        str(path),
        session_memory=(
            "<session_progress>\n"
            "Steps completed: 2\n\n"
            "Pages visited:\n"
            "  - example.com/admin (step 1, 2)\n\n"
            "Actions completed:\n"
            "  - Step 1: navigate to /admin → Navigated successfully\n"
            "  - Step 2: execute 3-step sequence → Step 1 complete\n"
            "Step 2 complete\n\n"
            "Failed attempts:\n"
            "  - Step 3: click '#missing'\n"
            "</session_progress>"
        ),
    )

    payload = json.loads(path.read_text())
    assert payload["session_memory"]["steps_completed"] == 2
    assert payload["session_memory"]["pages_visited"] == [
        {"page": "example.com/admin", "steps": [1, 2]}
    ]
    assert payload["session_memory"]["actions_completed"] == [
        {
            "step": 1,
            "summary": "navigate to /admin",
            "finding": "Navigated successfully",
        },
        {
            "step": 2,
            "summary": "execute 3-step sequence",
            "finding": "Step 1 complete\nStep 2 complete",
        },
    ]
    assert payload["session_memory"]["failed_attempts"] == [
        {"step": 3, "summary": "click '#missing'"}
    ]
    assert len(payload["actions"]) == 2
    assert payload["actions"][0]["step"] == 1
    assert payload["actions"][1]["step"] == 2
