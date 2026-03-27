"""Tests for agent tool schema exposure."""

from __future__ import annotations

from agent.tools import get_tools
from blinders.scope import ALL_ACTIONS


def test_browser_dom_tool_does_not_expose_select_or_evaluate():
    tool = get_tools()[0]
    actions = tool["input_schema"]["properties"]["action"]["enum"]
    assert "select" not in actions
    assert "evaluate" not in actions


def test_all_actions_stays_in_sync_with_tool_schema():
    tool = get_tools(allowed_actions=ALL_ACTIONS)[0]
    actions = set(tool["input_schema"]["properties"]["action"]["enum"])
    assert "select" not in actions
    assert "evaluate" not in actions


def test_execute_sequence_nested_actions_are_limited_to_safe_subset():
    tool = get_tools()[0]
    nested_actions = tool["input_schema"]["properties"]["steps"]["items"]["properties"][
        "action"
    ]["enum"]
    assert "select" not in nested_actions
    assert "evaluate" not in nested_actions
