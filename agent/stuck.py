"""Stuck detection for the agent loop.

Tracks recent actions and detects when the agent is repeating the same
action, injecting a hint to try a different approach.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)

MAX_RECENT = 6  # check last N actions for repetition
_STUCK_THRESHOLD = 4  # trigger hint after this many repeated signatures


class StuckDetector:
    """Detects when the agent is stuck repeating the same action."""

    def __init__(self) -> None:
        self._recent_actions: list[str] = []

    def record(self, tool_results: list[dict[str, Any]]) -> None:
        """Record action signatures from tool results."""
        for tr in tool_results:
            content = tr.get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    sig = item.get("text", "")[:80]
                    self._recent_actions.append(sig)
        if len(self._recent_actions) > MAX_RECENT:
            self._recent_actions[:] = self._recent_actions[-MAX_RECENT:]

    def get_hint(self) -> str | None:
        """Return a hint if the agent appears stuck, else None."""
        if len(self._recent_actions) < _STUCK_THRESHOLD:
            return None
        counts = Counter(self._recent_actions[-MAX_RECENT:])
        top_count = counts.most_common(1)[0][1]
        if top_count >= _STUCK_THRESHOLD:
            log.warning("Stuck detected: %d repeated actions", top_count)
            return (
                "\n\n[System] You appear stuck repeating the same action. "
                "Try a different approach: use a different selector, "
                "navigate to a different URL, or re-read the DOM."
            )
        return None
