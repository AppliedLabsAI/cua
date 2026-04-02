"""Tests for the system prompt builder."""

from __future__ import annotations

from agent.prompts import build_system_prompt
from credentials import SecretValue


class TestBuildSystemPrompt:
    def test_basic_prompt(self):
        prompt = build_system_prompt(directive="Go to example.com")
        assert "Go to example.com" in prompt
        assert "<task>" in prompt
        assert "<rules>" in prompt

    def test_includes_actions(self):
        prompt = build_system_prompt(directive="test")
        assert "goto" in prompt
        assert "click" in prompt
        assert "screenshot" in prompt
        assert "execute_sequence" in prompt

    def test_credentials_section(self):
        creds = {
            "username": SecretValue("testuser"),
            "password": SecretValue("testpass"),
        }
        prompt = build_system_prompt(directive="test", credentials=creds)
        assert "<robot_credentials>" in prompt
        assert "  username" in prompt
        assert "  password" in prompt
        assert "testuser" not in prompt
        assert "testpass" not in prompt
        assert "credential_ref" in prompt

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
        # Profile section should appear before <task>
        task_idx = prompt.index("<task>")
        profile_idx = prompt.index("## Research Mode")
        assert profile_idx < task_idx

    def test_no_profile(self):
        prompt = build_system_prompt(directive="test", profile_prompt=None)
        assert "<task>" in prompt

    def test_session_memory_included(self):
        memory_block = (
            "<session_progress>\nSteps completed: 2\n"
            "Pages visited:\n  - example.com (step 1)\n"
            "</session_progress>"
        )
        prompt = build_system_prompt(directive="test", session_memory=memory_block)
        assert "<session_progress>" in prompt
        assert "example.com (step 1)" in prompt
        # Memory should appear before <task>
        mem_idx = prompt.index("<session_progress>")
        task_idx = prompt.index("<task>")
        assert mem_idx < task_idx

    def test_no_session_memory(self):
        prompt = build_system_prompt(directive="test")
        assert "<session_progress>" not in prompt

    def test_do_not_revisit_rule(self):
        prompt = build_system_prompt(directive="test")
        assert "Do not revisit pages" in prompt

    def test_credentials_and_profile(self):
        creds = {"key": SecretValue("secret-value-123")}
        prompt = build_system_prompt(
            directive="test",
            credentials=creds,
            profile_prompt="## Custom\nDo stuff.",
        )
        assert "<robot_credentials>" in prompt
        assert "secret-value-123" not in prompt
        assert "## Custom" in prompt
        assert "<task>" in prompt
