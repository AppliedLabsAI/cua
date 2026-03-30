"""Tests for the stuck detection guardrail."""

from __future__ import annotations

from guardrails.stuck import StuckDetector, StuckSeverity


def _detector(**overrides) -> StuckDetector:
    return StuckDetector(**overrides)


class TestRepetitionDetection:
    def test_no_detection_with_varied_actions(self):
        det = _detector()
        actions = [f"click '#btn-{i}'" for i in range(8)]
        for a in actions:
            v = det.record(a, success=True)
            assert v.severity is StuckSeverity.NONE

    def test_hint_after_repeat_threshold(self):
        det = _detector(repeat_hint=3)
        det.record("click '#submit'", success=True)
        det.record("click '#submit'", success=True)
        v = det.record("click '#submit'", success=True)
        assert v.severity is StuckSeverity.HINT
        assert "[System]" in v.message

    def test_warning_after_warn_threshold(self):
        det = _detector(repeat_hint=3, repeat_warn=5)
        for _ in range(4):
            det.record("click '#submit'", success=True)
        v = det.record("click '#submit'", success=True)
        assert v.severity is StuckSeverity.WARNING
        assert "WARNING" in v.message

    def test_stop_after_stop_threshold(self):
        det = _detector(repeat_hint=3, repeat_warn=5, repeat_stop=7, window_size=10)
        for _ in range(6):
            det.record("click '#submit'", success=True)
        v = det.record("click '#submit'", success=True)
        assert v.severity is StuckSeverity.STOP
        assert "stopped" in v.message.lower()

    def test_window_trimming(self):
        det = _detector(window_size=4, repeat_hint=3)
        # Fill window with same action
        det.record("click '#a'", success=True)
        det.record("click '#a'", success=True)
        # Push old entries out with different actions
        det.record("click '#b'", success=True)
        det.record("click '#c'", success=True)
        det.record("click '#d'", success=True)
        # Now '#a' should only appear once (or not at all) in window
        v = det.record("click '#a'", success=True)
        assert v.severity is StuckSeverity.NONE

    def test_reset_clears_state(self):
        det = _detector(repeat_hint=3)
        for _ in range(3):
            det.record("click '#submit'", success=True)
        det.reset()
        # After reset, same action should not trigger
        v = det.record("click '#submit'", success=True)
        assert v.severity is StuckSeverity.NONE

    def test_mixed_success_and_failure(self):
        """Stuck detection works regardless of success/failure status."""
        det = _detector(repeat_hint=3)
        det.record("click '#submit'", success=True)
        det.record("click '#submit'", success=False)
        v = det.record("click '#submit'", success=True)
        assert v.severity is StuckSeverity.HINT


class TestCycleDetection:
    def test_cycle_length_2(self):
        det = _detector(cycle_max_length=3, cycle_repeats=3, window_size=10)
        for _ in range(3):
            det.record("click '#next'", success=True)
            det.record("click '#prev'", success=True)
        assert det.stuck_count >= 1

    def test_cycle_length_3(self):
        det = _detector(
            cycle_max_length=3, cycle_repeats=3, window_size=12, repeat_hint=10
        )
        for _ in range(3):
            det.record("click '#a'", success=True)
            det.record("click '#b'", success=True)
            det.record("click '#c'", success=True)
        assert det.stuck_count >= 1

    def test_no_cycle_with_varied_pattern(self):
        det = _detector(cycle_max_length=3, cycle_repeats=3, window_size=10)
        actions = ["click '#a'", "click '#b'", "click '#c'", "click '#d'"]
        for a in actions:
            v = det.record(a, success=True)
        assert v.severity is StuckSeverity.NONE

    def test_cycle_escalation(self):
        """Repeated cycle detections escalate severity."""
        det = _detector(
            cycle_max_length=2,
            cycle_repeats=3,
            window_size=12,
            repeat_hint=20,  # disable repetition detection
        )
        # First cycle detection -> HINT
        for _ in range(3):
            det.record("click '#x'", success=True)
            det.record("click '#y'", success=True)
        assert det.stuck_count >= 1

        # Second cycle detection -> WARNING
        for _ in range(3):
            det.record("click '#x'", success=True)
            det.record("click '#y'", success=True)
        assert det.stuck_count >= 2


class TestStuckCountDecay:
    def test_decay_on_non_stuck_actions(self):
        det = _detector(repeat_hint=3)
        # Trigger stuck detection
        for _ in range(3):
            det.record("click '#submit'", success=True)
        assert det.stuck_count == 1

        # Varied actions should decay the count
        det.record("click '#other'", success=True)
        assert det.stuck_count == 0
