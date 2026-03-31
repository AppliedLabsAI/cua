"""Lightweight circuit breaker for LLM API calls.

Prevents cascading failures when the LLM provider is down or rate-limiting.
Three states: CLOSED (normal), OPEN (fast-fail), HALF_OPEN (probe).
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum

from exceptions import LLMError

logger = logging.getLogger(__name__)


class CircuitOpenError(LLMError):
    """Raised when the circuit breaker is open and rejecting calls."""

    def __init__(self, failures: int, recovery_at: float) -> None:
        remaining = max(0, recovery_at - time.monotonic())
        super().__init__(
            f"Circuit breaker open after {failures} consecutive LLM failures. "
            f"Recovery in {remaining:.0f}s. The LLM provider may be unavailable."
        )
        self.failures = failures
        self.recovery_at = recovery_at


class _State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Process-local circuit breaker for LLM provider calls.

    Usage::

        breaker = CircuitBreaker()

        breaker.check()  # raises CircuitOpenError if open
        try:
            result = await llm_call()
            breaker.record_success()
        except SomeLLMError:
            breaker.record_failure()
            raise
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._state = _State.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0

    def _maybe_recover(self) -> None:
        """Transition OPEN -> HALF_OPEN once recovery timeout has elapsed."""
        if (
            self._state is _State.OPEN
            and time.monotonic() - self._opened_at >= self._recovery_timeout_s
        ):
            self._state = _State.HALF_OPEN

    @property
    def state(self) -> str:
        """Current state as a string (for logging/telemetry)."""
        self._maybe_recover()
        return self._state.value

    def check(self) -> None:
        """Allow a request through, or raise CircuitOpenError."""
        self._maybe_recover()
        if self._state is _State.OPEN:
            raise CircuitOpenError(
                self._consecutive_failures,
                self._opened_at + self._recovery_timeout_s,
            )

    def record_success(self) -> None:
        """Record a successful LLM call. Resets the circuit to CLOSED."""
        if self._consecutive_failures > 0 or self._state is not _State.CLOSED:
            logger.info(
                "Circuit breaker reset -> CLOSED (was %s, %d prior failures)",
                self._state.value,
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._state = _State.CLOSED

    def record_failure(self) -> None:
        """Record a failed LLM call. May trip the circuit to OPEN."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            if self._state is not _State.OPEN:
                logger.warning(
                    "Circuit breaker tripped -> OPEN after %d consecutive failures "
                    "(recovery in %ds)",
                    self._consecutive_failures,
                    self._recovery_timeout_s,
                )
                self._opened_at = time.monotonic()
            self._state = _State.OPEN
