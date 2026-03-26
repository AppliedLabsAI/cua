"""Tests for agent.stuck — stuck detection."""

from agent.stuck import StuckDetector


class TestStuckDetector:
    def _tool_result(self, text: str) -> dict:
        return {"content": [{"type": "text", "text": text}]}

    def test_no_hint_when_not_stuck(self):
        d = StuckDetector()
        d.record([self._tool_result("action 1")])
        d.record([self._tool_result("action 2")])
        d.record([self._tool_result("action 3")])
        assert d.get_hint() is None

    def test_hint_when_repeating_same_action(self):
        d = StuckDetector()
        for _ in range(5):
            d.record([self._tool_result("Clicked button.submit")])
        hint = d.get_hint()
        assert hint is not None
        assert "stuck" in hint.lower()

    def test_no_hint_with_varied_actions(self):
        d = StuckDetector()
        for i in range(6):
            d.record([self._tool_result(f"Action {i}")])
        assert d.get_hint() is None

    def test_tracks_only_recent_actions(self):
        d = StuckDetector()
        # Fill with varied actions
        for i in range(10):
            d.record([self._tool_result(f"Unique action {i}")])
        # Now it shouldn't be stuck since all are unique within window
        assert d.get_hint() is None

    def test_handles_empty_content(self):
        d = StuckDetector()
        d.record([{"content": []}])
        d.record([{}])
        assert d.get_hint() is None

    def test_hint_after_threshold_of_same_signature(self):
        d = StuckDetector()
        # 3 different, then 4 same
        d.record([self._tool_result("different 1")])
        d.record([self._tool_result("different 2")])
        for _ in range(4):
            d.record([self._tool_result("same action")])
        hint = d.get_hint()
        assert hint is not None
