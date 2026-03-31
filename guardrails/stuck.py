"""Stuck detection for the CUA agent.

Detects when the agent repeats the same action or cycles between a small
set of actions. Integrated via GuardrailEngine.record_action(), called
from ActionRouter after every tool execution.

Two detection strategies on a sliding window of action signatures:
  1. Same-action repetition (click '#btn' 5 times in a row)
  2. Cycle detection (A -> B -> A -> B pattern)

Escalation: HINT (gentle nudge) -> WARNING (strong) -> STOP (hard stop).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class StuckSeverity(Enum):
    """Escalation tiers for stuck detection."""

    NONE = "none"
    HINT = "hint"
    WARNING = "warning"
    STOP = "stop"


@dataclass(frozen=True)
class StuckVerdict:
    """Result of a stuck detection check."""

    severity: StuckSeverity = StuckSeverity.NONE
    message: str = ""


@dataclass(frozen=True)
class ActionSignature:
    """Compact identity for stuck detection."""

    key: str
    summary: str


_HINT_MSG = (
    "[System] You appear to be repeating the same action. "
    "Try a different approach: use a different selector, "
    "navigate to a different URL, or re-read the DOM."
)
_WARN_MSG = (
    "[System] WARNING: You are stuck in a loop repeating the same action. "
    "You MUST try a completely different strategy immediately. "
    "Do NOT repeat this action again."
)
_STOP_MSG = (
    "[System] Agent stopped: stuck in a loop after repeated identical actions. "
    "Summarize what you accomplished and any remaining steps needed."
)
_CYCLE_HINT_MSG = (
    "[System] You appear to be cycling between the same actions. "
    "This pattern is not making progress. Try a different approach entirely."
)
_CYCLE_WARN_MSG = (
    "[System] WARNING: You are stuck in an action cycle. "
    "You MUST break the pattern and try a completely different strategy."
)
_CYCLE_STOP_MSG = (
    "[System] Agent stopped: stuck in an action cycle with no progress. "
    "Summarize what you accomplished and any remaining steps needed."
)


class StuckDetector:
    """Detects when the agent is stuck repeating actions or cycling."""

    def __init__(
        self,
        *,
        window_size: int = 8,
        repeat_hint: int = 3,
        repeat_warn: int = 5,
        repeat_stop: int = 7,
        cycle_max_length: int = 3,
        cycle_repeats: int = 3,
    ) -> None:
        # How many recent actions to keep for pattern analysis
        self._window_size = window_size
        # Same action repeated N times in window → gentle hint
        self._repeat_hint = repeat_hint
        # Same action repeated N times in window → strong warning
        self._repeat_warn = repeat_warn
        # Same action repeated N times in window → hard stop
        self._repeat_stop = repeat_stop
        # Maximum cycle pattern length to check (e.g. 3 = A-B-C)
        self._cycle_max_length = cycle_max_length
        # How many times a cycle must repeat to trigger detection
        self._cycle_repeats = cycle_repeats
        # Sliding window of normalized action signatures.
        self._history: list[ActionSignature] = []
        # Cumulative detection count — drives cycle escalation and
        # decays by 1 on each non-stuck action
        self._stuck_count: int = 0

    def record(
        self,
        action: str,
        tool_input: dict,
        *,
        input_summary: str,
        success: bool,
    ) -> StuckVerdict:
        """Record an action and check for stuck patterns.

        Returns a verdict with severity and message. The caller decides
        how to act on it (prepend hint, replace result, etc.).
        """
        self._history.append(
            ActionSignature(
                key=build_action_signature(action, tool_input),
                summary=input_summary,
            )
        )
        if len(self._history) > self._window_size:
            self._history = self._history[-self._window_size :]

        # Need minimum history for any detection
        min_needed = min(self._repeat_hint, 2 * self._cycle_repeats)
        if len(self._history) < min_needed:
            return StuckVerdict()

        # Strategy 1: same-action repetition
        rep = self._check_repetition()
        if rep.severity is not StuckSeverity.NONE:
            self._stuck_count += 1
            return rep

        # Strategy 2: cycle detection
        cyc = self._check_cycle()
        if cyc.severity is not StuckSeverity.NONE:
            self._stuck_count += 1
            return cyc

        # No stuck pattern — decay cumulative count
        if self._stuck_count > 0:
            self._stuck_count -= 1

        return StuckVerdict()

    @property
    def stuck_count(self) -> int:
        """Cumulative detection count (drives cycle escalation)."""
        return self._stuck_count

    def reset(self) -> None:
        """Clear all state."""
        self._history.clear()
        self._stuck_count = 0

    def _check_repetition(self) -> StuckVerdict:
        """Check if the most recent action repeats too many times in the window."""
        if not self._history:
            return StuckVerdict()

        latest = self._history[-1].key
        count = 0
        for sig in reversed(self._history):
            if sig.key != latest:
                break
            count += 1

        if count >= self._repeat_stop:
            return StuckVerdict(StuckSeverity.STOP, _STOP_MSG)
        if count >= self._repeat_warn:
            return StuckVerdict(StuckSeverity.WARNING, _WARN_MSG)
        if count >= self._repeat_hint:
            return StuckVerdict(StuckSeverity.HINT, _HINT_MSG)

        return StuckVerdict()

    def _check_cycle(self) -> StuckVerdict:
        """Check if the tail of history repeats a short cycle pattern."""
        history = [entry.key for entry in self._history]
        n = len(history)

        for cycle_len in range(2, self._cycle_max_length + 1):
            if n < cycle_len * self._cycle_repeats:
                continue

            # Extract candidate cycle from the tail
            candidate = history[-cycle_len:]

            # Count how many consecutive times the cycle repeats backward
            repeats = 0
            for i in range(n - cycle_len, -1, -cycle_len):
                chunk = history[i : i + cycle_len]
                if chunk == candidate:
                    repeats += 1
                else:
                    break

            if repeats >= self._cycle_repeats:
                # Escalate based on cumulative stuck count
                if self._stuck_count >= 2:
                    return StuckVerdict(StuckSeverity.STOP, _CYCLE_STOP_MSG)
                if self._stuck_count >= 1:
                    return StuckVerdict(StuckSeverity.WARNING, _CYCLE_WARN_MSG)
                return StuckVerdict(StuckSeverity.HINT, _CYCLE_HINT_MSG)

        return StuckVerdict()


def build_action_signature(action: str, tool_input: dict) -> str:
    """Build a normalized action identity for stuck detection."""
    normalized_action = action.strip().lower()
    if normalized_action == "goto":
        return f"goto|{tool_input.get('url', '').strip().lower()}"
    if normalized_action in {"click", "extract", "wait_for", "select"}:
        selector = str(tool_input.get("selector", "")).strip().lower()
        return f"{normalized_action}|{selector}"
    if normalized_action == "key_press":
        selector = str(tool_input.get("selector", "")).strip().lower()
        key = str(tool_input.get("key", "")).strip().lower()
        credential_ref = str(tool_input.get("credential_ref", "")).strip().lower()
        text_present = "text" if bool(tool_input.get("text")) else ""
        return (
            f"key_press|selector={selector}|key={key}|"
            f"credential_ref={credential_ref}|payload={text_present}"
        )
    if normalized_action == "scroll":
        direction = str(tool_input.get("direction", "down")).strip().lower()
        amount = str(tool_input.get("amount", ""))
        return f"scroll|{direction}|{amount}"
    if normalized_action == "execute_sequence":
        steps = tool_input.get("steps", [])
        parts = []
        for step in steps:
            if isinstance(step, dict):
                step_action = str(step.get("action", "")).strip().lower()
                parts.append(build_action_signature(step_action, step))
        return "execute_sequence|" + "->".join(parts)
    return normalized_action
