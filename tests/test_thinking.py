"""Tests for agent.thinking — adaptive thinking budget."""

from agent.thinking import AdaptiveThinking


class TestAdaptiveThinking:
    def test_first_two_steps_use_full_budget(self):
        t = AdaptiveThinking(base=4096, reduced=1024)
        assert t.budget == 4096  # step 1
        assert t.budget == 4096  # step 2

    def test_reduced_budget_after_consecutive_successes(self):
        t = AdaptiveThinking(base=4096, reduced=1024)
        _ = t.budget  # step 1
        _ = t.budget  # step 2
        for _ in range(3):
            t.record(True)
        assert t.budget == 1024  # step 3, after 3 successes

    def test_error_resets_to_full_budget(self):
        t = AdaptiveThinking(base=4096, reduced=1024)
        _ = t.budget  # step 1
        _ = t.budget  # step 2
        for _ in range(3):
            t.record(True)
        t.record(False)  # error resets
        assert t.budget == 4096  # step 3, back to full

    def test_full_budget_with_few_successes(self):
        t = AdaptiveThinking(base=4096, reduced=1024)
        _ = t.budget  # step 1
        _ = t.budget  # step 2
        t.record(True)
        t.record(True)
        # Only 2 successes, need 3 for reduced
        assert t.budget == 4096

    def test_successes_cap_at_5(self):
        t = AdaptiveThinking(base=4096, reduced=1024)
        for _ in range(10):
            t.record(True)
        # Internal counter should cap at 5, not overflow
        _ = t.budget  # step 1
        _ = t.budget  # step 2
        assert t.budget == 1024  # step 3
