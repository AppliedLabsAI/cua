"""Tests for agent.context — conversation pruning and truncation."""

from agent.context import MAX_RESULT_CHARS, prune_old_context, truncate_tool_result
from bridge import DOM_MARKER


class TestPruneOldContext:
    def _msg(self, role, content):
        return {"role": role, "content": content}

    def test_removes_old_screenshots_keeps_last(self):
        messages = [
            self._msg("user", [{"type": "image", "source": {"data": "img1"}}]),
            self._msg("user", [{"type": "image", "source": {"data": "img2"}}]),
            self._msg("user", [{"type": "image", "source": {"data": "img3"}}]),
        ]
        prune_old_context(messages, keep_last=1)
        # First two should be replaced
        assert messages[0]["content"][0]["text"] == "[old screenshot removed]"
        assert messages[1]["content"][0]["text"] == "[old screenshot removed]"
        # Last one kept
        assert messages[2]["content"][0]["type"] == "image"

    def test_truncates_old_dom_snapshots(self):
        messages = [
            self._msg("user", [{"type": "text", "text": f"Page 1\n{DOM_MARKER}\n<div>old</div>"}]),
            self._msg("user", [{"type": "text", "text": f"Page 2\n{DOM_MARKER}\n<div>new</div>"}]),
        ]
        prune_old_context(messages, keep_last=1)
        # First DOM should be truncated
        assert "[DOM removed]" in messages[0]["content"][0]["text"]
        assert "old" not in messages[0]["content"][0]["text"]
        # Last DOM kept
        assert "<div>new</div>" in messages[1]["content"][0]["text"]

    def test_no_crash_on_empty_messages(self):
        messages = []
        prune_old_context(messages)
        assert messages == []

    def test_truncates_old_user_text(self):
        long_text = "A" * 200
        messages = [
            self._msg("user", [{"type": "text", "text": long_text}]),
            self._msg("user", [{"type": "text", "text": "recent1"}]),
            self._msg("user", [{"type": "text", "text": "recent2"}]),
        ]
        prune_old_context(messages, keep_last=2)
        # Old user message text should be truncated to first 80 chars
        assert len(messages[0]["content"][0]["text"]) == 80


class TestTruncateToolResult:
    def test_truncates_long_text(self):
        text = "x" * (MAX_RESULT_CHARS + 500)
        result = {"content": [{"type": "text", "text": text}]}
        truncated = truncate_tool_result(result, "extract")
        out_text = truncated["content"][0]["text"]
        assert len(out_text) < len(text)
        assert "truncated" in out_text

    def test_leaves_short_text_unchanged(self):
        result = {"content": [{"type": "text", "text": "short"}]}
        assert truncate_tool_result(result, "click") is result

    def test_preserves_non_text_content(self):
        result = {"content": [{"type": "image", "source": {"data": "abc"}}]}
        assert truncate_tool_result(result, "screenshot") is result

    def test_handles_empty_content(self):
        result = {"content": []}
        assert truncate_tool_result(result, "click") is result

    def test_handles_missing_content(self):
        result = {}
        assert truncate_tool_result(result, "click") is result
