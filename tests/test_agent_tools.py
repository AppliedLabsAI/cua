"""Tests for agent tool schema exposure."""

from __future__ import annotations

from agent.tools import _NESTED_ACTIONS, get_action_enum
from blinders.scope import ALL_ACTIONS


def test_browser_dom_tool_does_not_expose_select_or_evaluate():
    actions = get_action_enum()
    assert "select" not in actions
    assert "evaluate" not in actions


def test_all_actions_stays_in_sync_with_tool_schema():
    actions = get_action_enum(allowed_actions=ALL_ACTIONS)
    assert "select" not in actions
    assert "evaluate" not in actions


def test_execute_sequence_nested_actions_are_limited_to_safe_subset():
    assert "select" not in _NESTED_ACTIONS
    assert "evaluate" not in _NESTED_ACTIONS
    assert "execute_sequence" not in _NESTED_ACTIONS
