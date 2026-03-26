"""Adaptive thinking budget for the agent loop."""

from __future__ import annotations


class AdaptiveThinking:
    """Scale thinking budget based on consecutive successes and step count.

    Early steps get full budget for planning. After consecutive successes,
    budget drops since the agent is in a known-good execution flow.
    Errors reset to full budget for recovery reasoning.
    """

    def __init__(self, base: int = 1024, reduced: int = 1024) -> None:
        self.base = base
        self.reduced = reduced
        self._successes = 0
        self._step = 0

    @property
    def budget(self) -> int:
        self._step += 1
        # First 2 steps: full budget for task planning
        if self._step <= 2:
            return self.base
        # After 3+ consecutive successes: reduced budget
        if self._successes >= 3:
            return self.reduced
        return self.base

    def record(self, success: bool) -> None:
        if success:
            self._successes = min(self._successes + 1, 5)
        else:
            self._successes = 0
