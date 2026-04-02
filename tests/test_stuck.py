"""Tests for the stuck detection guardrail."""

from __future__ import annotations

from guardrails.stuck import StuckDetector, StuckSeverity, build_action_signature


def _detector(**overrides) -> StuckDetector:
    return StuckDetector(**overrides)


def _record(
    det: StuckDetector,
    summary: str,
    *,
    success: bool = True,
    visited_urls: list[str] | None = None,
):
    return det.record(
        "click",
        {"selector": summary.split("'")[1]},
        input_summary=summary,
        success=success,
        visited_urls=visited_urls,
    )


class TestRepetitionDetection:
    def test_no_detection_with_varied_actions(self):
        det = _detector()
        actions = [f"click '#btn-{i}'" for i in range(8)]
        for a in actions:
            v = _record(det, a, success=True)
            assert v.severity is StuckSeverity.NONE

    def test_hint_after_repeat_threshold(self):
        det = _detector(repeat_hint=3)
        _record(det, "click '#submit'", success=True)
        _record(det, "click '#submit'", success=True)
        v = _record(det, "click '#submit'", success=True)
        assert v.severity is StuckSeverity.HINT
        assert "[System]" in v.message

    def test_warning_after_warn_threshold(self):
        det = _detector(repeat_hint=3, repeat_warn=5)
        for _ in range(4):
            _record(det, "click '#submit'", success=True)
        v = _record(det, "click '#submit'", success=True)
        assert v.severity is StuckSeverity.WARNING
        assert "WARNING" in v.message

    def test_stop_after_stop_threshold(self):
        det = _detector(repeat_hint=3, repeat_warn=5, repeat_stop=7, window_size=10)
        for _ in range(6):
            _record(det, "click '#submit'", success=True)
        v = _record(det, "click '#submit'", success=True)
        assert v.severity is StuckSeverity.STOP
        assert "stopped" in v.message.lower()

    def test_window_trimming(self):
        det = _detector(window_size=4, repeat_hint=3)
        # Fill window with same action
        _record(det, "click '#a'", success=True)
        _record(det, "click '#a'", success=True)
        # Push old entries out with different actions
        _record(det, "click '#b'", success=True)
        _record(det, "click '#c'", success=True)
        _record(det, "click '#d'", success=True)
        # Now '#a' should only appear once (or not at all) in window
        v = _record(det, "click '#a'", success=True)
        assert v.severity is StuckSeverity.NONE

    def test_reset_clears_state(self):
        det = _detector(repeat_hint=3)
        for _ in range(3):
            _record(det, "click '#submit'", success=True)
        det.reset()
        # After reset, same action should not trigger
        v = _record(det, "click '#submit'", success=True)
        assert v.severity is StuckSeverity.NONE

    def test_mixed_success_and_failure(self):
        """Stuck detection works regardless of success/failure status."""
        det = _detector(repeat_hint=3)
        _record(det, "click '#submit'", success=True)
        _record(det, "click '#submit'", success=False)
        v = _record(det, "click '#submit'", success=True)
        assert v.severity is StuckSeverity.HINT

    def test_non_consecutive_repeats_do_not_trigger(self):
        det = _detector(repeat_hint=3, window_size=8)
        _record(det, "click '#submit'")
        _record(det, "click '#other'")
        _record(det, "click '#submit'")
        _record(det, "click '#other'")
        v = _record(det, "click '#submit'")
        assert v.severity is StuckSeverity.NONE


class TestCycleDetection:
    def test_cycle_length_2(self):
        det = _detector(cycle_max_length=3, cycle_repeats=3, window_size=10)
        for _ in range(3):
            _record(det, "click '#next'", success=True)
            _record(det, "click '#prev'", success=True)
        assert det.stuck_count >= 1

    def test_cycle_length_3(self):
        det = _detector(
            cycle_max_length=3, cycle_repeats=3, window_size=12, repeat_hint=10
        )
        for _ in range(3):
            _record(det, "click '#a'", success=True)
            _record(det, "click '#b'", success=True)
            _record(det, "click '#c'", success=True)
        assert det.stuck_count >= 1

    def test_no_cycle_with_varied_pattern(self):
        det = _detector(cycle_max_length=3, cycle_repeats=3, window_size=10)
        actions = ["click '#a'", "click '#b'", "click '#c'", "click '#d'"]
        for a in actions:
            v = _record(det, a, success=True)
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
            _record(det, "click '#x'", success=True)
            _record(det, "click '#y'", success=True)
        assert det.stuck_count >= 1

        # Second cycle detection -> WARNING
        for _ in range(3):
            _record(det, "click '#x'", success=True)
            _record(det, "click '#y'", success=True)
        assert det.stuck_count >= 2


class TestUrlRevisitDetection:
    def _goto(self, det: StuckDetector, url: str, *, success: bool = True):
        return det.record(
            "goto",
            {"url": url},
            input_summary=f"navigate to {url}",
            success=success,
        )

    def _click(self, det: StuckDetector, selector: str):
        return det.record(
            "click",
            {"selector": selector},
            input_summary=f"click '{selector}'",
            success=True,
        )

    def test_no_detection_on_first_visit(self):
        det = _detector(revisit_gap=3)
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.NONE

    def test_revisit_within_gap_not_detected(self):
        det = _detector(revisit_gap=5)
        self._goto(det, "https://example.com")
        self._click(det, "#a")
        self._click(det, "#b")
        # Only 3 intervening actions — below gap of 5
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.NONE

    def test_revisit_beyond_gap_detected(self):
        det = _detector(revisit_gap=5)
        self._goto(det, "https://example.com/dashboard")
        for i in range(5):
            self._click(det, f"#btn-{i}")
        v = self._goto(det, "https://example.com/dashboard")
        assert v.severity is StuckSeverity.HINT
        assert "already visited" in v.message.lower()

    def test_revisit_escalation_when_rapid(self):
        """Rapid revisits (no decay gap) escalate to WARNING."""
        det = _detector(revisit_gap=3, repeat_hint=20)
        self._goto(det, "https://example.com")
        for i in range(4):
            self._click(det, f"#a-{i}")
        # First revisit → HINT (stuck_count becomes 1)
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.HINT
        # Second revisit → HINT again (stuck_count becomes 2)
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.HINT
        # Third revisit → WARNING (stuck_count >= 2)
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.WARNING

    def test_revisit_does_not_escalate_when_separated(self):
        """Revisits separated by many non-stuck actions decay back to HINT."""
        det = _detector(revisit_gap=3, repeat_hint=20)
        self._goto(det, "https://example.com")
        for i in range(4):
            self._click(det, f"#a-{i}")
        # First revisit → HINT
        self._goto(det, "https://example.com")
        # Intervening actions decay stuck_count back toward 0
        for i in range(4):
            self._click(det, f"#b-{i}")
        # Second revisit after decay → HINT again, not WARNING
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.HINT

    def test_url_normalization(self):
        det = _detector(revisit_gap=3)
        self._goto(det, "https://Example.COM/Path/")
        for i in range(4):
            self._click(det, f"#x-{i}")
        v = self._goto(det, "https://example.com/Path")
        assert v.severity is StuckSeverity.HINT

    def test_different_urls_not_detected(self):
        det = _detector(revisit_gap=3)
        self._goto(det, "https://example.com/page-a")
        for i in range(4):
            self._click(det, f"#x-{i}")
        v = self._goto(det, "https://example.com/page-b")
        assert v.severity is StuckSeverity.NONE

    def test_failed_goto_not_tracked(self):
        det = _detector(revisit_gap=3)
        self._goto(det, "https://example.com", success=False)
        for i in range(4):
            self._click(det, f"#x-{i}")
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.NONE

    def test_execute_sequence_with_goto_tracked(self):
        det = _detector(revisit_gap=3)
        det.record(
            "execute_sequence",
            {"steps": [{"action": "goto", "url": "https://example.com"}]},
            input_summary="execute 1-step sequence",
            success=True,
        )
        for i in range(4):
            self._click(det, f"#x-{i}")
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.HINT

    def test_reset_clears_url_history(self):
        det = _detector(revisit_gap=3)
        self._goto(det, "https://example.com")
        for i in range(4):
            self._click(det, f"#x-{i}")
        det.reset()
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.NONE

    def test_revisit_does_not_refire_on_subsequent_actions(self):
        """After a revisit is detected, subsequent non-goto actions should not re-trigger."""
        det = _detector(revisit_gap=3, repeat_hint=20)
        self._goto(det, "https://example.com")
        for i in range(4):
            self._click(det, f"#a-{i}")
        v = self._goto(det, "https://example.com")
        assert v.severity is StuckSeverity.HINT
        # Subsequent clicks should NOT re-fire the revisit
        v = self._click(det, "#next-action")
        assert v.severity is StuckSeverity.NONE
        v = self._click(det, "#another-action")
        assert v.severity is StuckSeverity.NONE

    def test_click_navigation_revisit_detected_from_actual_visited_url(self):
        det = _detector(revisit_gap=3, repeat_hint=20)
        _record(
            det,
            "click '#nav-home'",
            visited_urls=["https://example.com/dashboard"],
        )
        for i in range(4):
            _record(det, f"click '#x-{i}'", success=True)
        v = _record(
            det,
            "click '#nav-home-again'",
            visited_urls=["https://example.com/dashboard"],
        )
        assert v.severity is StuckSeverity.HINT

    def test_query_variants_are_not_treated_as_same_page(self):
        det = _detector(revisit_gap=3, repeat_hint=20)
        _record(
            det,
            "click '#search-alpha'",
            visited_urls=["https://example.com/search?q=alpha"],
        )
        for i in range(4):
            _record(det, f"click '#x-{i}'", success=True)
        v = _record(
            det,
            "click '#search-beta'",
            visited_urls=["https://example.com/search?q=beta"],
        )
        assert v.severity is StuckSeverity.NONE


class TestStuckCountDecay:
    def test_decay_on_non_stuck_actions(self):
        det = _detector(repeat_hint=3)
        # Trigger stuck detection
        for _ in range(3):
            _record(det, "click '#submit'", success=True)
        assert det.stuck_count == 1

        # Varied actions should decay the count
        _record(det, "click '#other'", success=True)
        assert det.stuck_count == 0


def test_build_action_signature_uses_action_and_selector():
    sig = build_action_signature("click", {"selector": "#submit"})
    assert sig == "click|#submit"
