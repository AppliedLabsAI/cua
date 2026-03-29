"""Tests for the system prompt builder."""

from __future__ import annotations

from agent.prompts import build_system_prompt
from credentials import SecretValue


class TestBuildSystemPrompt:
    def test_basic_prompt(self):
        prompt = build_system_prompt(directive="Go to example.com")
        assert "Go to example.com" in prompt
        assert "## Task" in prompt
        assert "## Rules" in prompt

    def test_includes_actions(self):
        prompt = build_system_prompt(directive="test")
        assert "goto" in prompt
        assert "click" in prompt
        assert "screenshot" in prompt
        assert "execute_sequence" in prompt

    def test_credentials_section(self):
        creds = {
            "github": {
                "username": SecretValue("testuser"),
                "password": SecretValue("testpass"),
            },
        }
        prompt = build_system_prompt(directive="test", credentials=creds)
        assert "<robot_credentials>" in prompt
        assert "github:" in prompt
        assert "username: testuser" in prompt
        assert "password: testpass" in prompt

    def test_no_credentials(self):
        prompt = build_system_prompt(directive="test")
        assert "<robot_credentials>" not in prompt

    def test_profile_prompt_extension(self):
        prompt = build_system_prompt(
            directive="test",
            profile_prompt="## Research Mode\nRead thoroughly.",
        )
        assert "## Research Mode" in prompt
        assert "Read thoroughly." in prompt
        # Profile section should appear before ## Task
        task_idx = prompt.index("## Task")
        profile_idx = prompt.index("## Research Mode")
        assert profile_idx < task_idx

    def test_no_profile(self):
        prompt = build_system_prompt(directive="test", profile_prompt=None)
        assert "## Task" in prompt

    def test_credentials_and_profile(self):
        creds = {"svc": {"key": SecretValue("val")}}
        prompt = build_system_prompt(
            directive="test",
            credentials=creds,
            profile_prompt="## Custom\nDo stuff.",
        )
        assert "<robot_credentials>" in prompt
        assert "## Custom" in prompt
        assert "## Task" in prompt
