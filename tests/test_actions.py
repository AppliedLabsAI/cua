"""Tests for action log utilities."""

from __future__ import annotations

from actionlog.actions import _sanitize_tool_input, summarize_action


class TestSummarizeAction:
    def test_goto(self):
        result = summarize_action("browser_dom", "goto", {"url": "https://example.com"})
        assert "navigate to" in result
        assert "example.com" in result

    def test_click(self):
        result = summarize_action("browser_dom", "click", {"selector": "#btn"})
        assert "click" in result
        assert "#btn" in result

    def test_key_press_text_and_key(self):
        result = summarize_action(
            "browser_dom", "key_press", {"text": "hello", "key": "Enter"}
        )
        assert "type" in result
        assert "hello" in result
        assert "Enter" in result

    def test_key_press_key_only(self):
        result = summarize_action("browser_dom", "key_press", {"key": "Tab"})
        assert "press" in result
        assert "Tab" in result

    def test_key_press_credential_ref_masks_secret(self):
        result = summarize_action(
            "browser_dom",
            "key_press",
            {"credential_ref": "password", "key": "Enter"},
        )
        assert "credential" in result
        assert "password" in result
        assert "Enter" in result

    def test_scroll(self):
        result = summarize_action(
            "browser_dom", "scroll", {"direction": "down", "amount": 5}
        )
        assert "scroll" in result
        assert "down" in result

    def test_extract(self):
        result = summarize_action("browser_dom", "extract", {"selector": ".content"})
        assert "extract" in result

    def test_execute_sequence(self):
        steps = [{"action": "click"}, {"action": "key_press"}, {"action": "click"}]
        result = summarize_action("browser_dom", "execute_sequence", {"steps": steps})
        assert "3-step" in result

    def test_screenshot(self):
        result = summarize_action("browser_dom", "screenshot", {})
        assert "screenshot" in result

    def test_long_selector_truncated(self):
        long_sel = "a" * 100
        result = summarize_action("browser_dom", "click", {"selector": long_sel})
        assert "..." in result

    def test_unknown_tool(self):
        result = summarize_action("other_tool", "do_thing", {})
        assert "other_tool.do_thing" in result


class TestSanitizeToolInput:
    def test_truncates_text_field(self):
        inp = {"text": "x" * 1000, "action": "key_press"}
        result = _sanitize_tool_input(inp)
        assert len(result["text"]) < 1000
        assert "chars total" in result["text"]

    def test_leaves_short_text(self):
        inp = {"text": "hello", "action": "key_press"}
        result = _sanitize_tool_input(inp)
        assert result["text"] == "hello"

    def test_non_large_fields_untouched(self):
        inp = {"action": "goto", "url": "x" * 1000}
        result = _sanitize_tool_input(inp)
        assert result["url"] == "x" * 1000

    def test_truncates_nested_execute_sequence_text(self):
        inp = {
            "action": "execute_sequence",
            "steps": [
                {"action": "key_press", "text": "x" * 1000},
            ],
        }
        result = _sanitize_tool_input(inp)
        assert len(result["steps"][0]["text"]) < 1000
        assert "chars total" in result["steps"][0]["text"]
