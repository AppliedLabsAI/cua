"""Tests for agent/circuit_breaker.py — lightweight circuit breaker for LLM calls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.circuit_breaker import CircuitBreaker, CircuitOpenError
from exceptions import LLMError

# ── Construction ─────────────────────────────────────────────────────────────


def test_default_thresholds():
    cb = CircuitBreaker()
    assert cb._failure_threshold == 3
    assert cb._recovery_timeout_s == 30.0


def test_custom_thresholds():
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout_s=60.0)
    assert cb._failure_threshold == 5
    assert cb._recovery_timeout_s == 60.0


def test_initial_state_is_closed():
    cb = CircuitBreaker()
    assert cb.state == "closed"


def test_initial_consecutive_failures_is_zero():
    cb = CircuitBreaker()
    assert cb._consecutive_failures == 0


# ── state property ────────────────────────────────────────────────────────────


def test_state_returns_closed_string_when_closed():
    cb = CircuitBreaker()
    assert cb.state == "closed"


def test_state_returns_open_string_when_open():
    cb = CircuitBreaker(failure_threshold=1)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
        assert cb.state == "open"


def test_state_returns_half_open_string_after_recovery_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    # Advance past recovery timeout
    with patch("agent.circuit_breaker.time.monotonic", return_value=131.0):
        assert cb.state == "half_open"


# ── check() in CLOSED state ───────────────────────────────────────────────────


def test_check_passes_when_closed():
    cb = CircuitBreaker()
    cb.check()  # must not raise


def test_check_passes_when_closed_after_some_failures_below_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.check()  # still CLOSED, must not raise


# ── record_failure() below threshold keeps CLOSED ─────────────────────────────


def test_record_failure_below_threshold_stays_closed():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    assert cb.state == "closed"


def test_record_failure_increments_consecutive_failures():
    cb = CircuitBreaker(failure_threshold=5)
    cb.record_failure()
    cb.record_failure()
    assert cb._consecutive_failures == 2


def test_record_failure_at_threshold_minus_one_stays_closed():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"


# ── record_failure() at threshold trips to OPEN ───────────────────────────────


def test_record_failure_at_threshold_trips_to_open():
    cb = CircuitBreaker(failure_threshold=3)
    with patch("agent.circuit_breaker.time.monotonic", return_value=500.0):
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"


def test_check_raises_circuit_open_error_when_open():
    cb = CircuitBreaker(failure_threshold=1)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    with (
        patch("agent.circuit_breaker.time.monotonic", return_value=100.0),
        pytest.raises(CircuitOpenError),
    ):
        cb.check()


def test_record_failure_records_opened_at_timestamp():
    cb = CircuitBreaker(failure_threshold=1)
    with patch("agent.circuit_breaker.time.monotonic", return_value=999.0):
        cb.record_failure()
    assert cb._opened_at == 999.0


def test_additional_failure_while_open_does_not_reset_opened_at():
    """A second trip from HALF_OPEN must not reset _opened_at by re-calling monotonic."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()  # trips OPEN, _opened_at = 100.0

    # Advance to HALF_OPEN
    with patch("agent.circuit_breaker.time.monotonic", return_value=131.0):
        cb.check()  # transitions to HALF_OPEN
        cb.record_failure()  # re-trips to OPEN; _opened_at must be updated to 131.0

    assert cb._state.value == "open"
    # The new _opened_at should be the time of the HALF_OPEN failure, not the original
    assert cb._opened_at == 131.0


# ── record_success() resets to CLOSED ────────────────────────────────────────


def test_record_success_from_closed_stays_closed():
    cb = CircuitBreaker()
    cb.record_success()
    assert cb.state == "closed"


def test_record_success_after_failures_resets_to_closed():
    cb = CircuitBreaker(failure_threshold=5)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.state == "closed"


def test_record_success_resets_consecutive_failures_to_zero():
    cb = CircuitBreaker(failure_threshold=5)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb._consecutive_failures == 0


def test_record_success_from_open_resets_to_closed():
    cb = CircuitBreaker(failure_threshold=1)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    assert cb._state.value == "open"
    cb.record_success()
    assert cb.state == "closed"


def test_record_success_from_half_open_resets_to_closed():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    with patch("agent.circuit_breaker.time.monotonic", return_value=131.0):
        cb.check()  # transitions to HALF_OPEN
    assert cb._state.value == "half_open"
    cb.record_success()
    assert cb.state == "closed"
    assert cb._consecutive_failures == 0


# ── OPEN -> HALF_OPEN transition after recovery timeout ───────────────────────


def test_open_does_not_transition_to_half_open_before_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    # Only 29 seconds elapsed — not enough
    with patch("agent.circuit_breaker.time.monotonic", return_value=129.9):
        assert cb.state == "open"


def test_open_transitions_to_half_open_exactly_at_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    with patch("agent.circuit_breaker.time.monotonic", return_value=130.0):
        assert cb.state == "half_open"


def test_open_transitions_to_half_open_after_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=0.0):
        cb.record_failure()
    with patch("agent.circuit_breaker.time.monotonic", return_value=60.0):
        assert cb.state == "half_open"


# ── check() in HALF_OPEN allows request ───────────────────────────────────────


def test_check_in_half_open_does_not_raise():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    with patch("agent.circuit_breaker.time.monotonic", return_value=131.0):
        cb.check()  # must not raise — probe request is allowed


# ── record_failure() in HALF_OPEN re-trips to OPEN ────────────────────────────


def test_record_failure_in_half_open_trips_back_to_open():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    # Advance to HALF_OPEN
    with patch("agent.circuit_breaker.time.monotonic", return_value=131.0):
        cb.check()  # triggers transition to HALF_OPEN
        assert cb._state.value == "half_open"
        cb.record_failure()  # probe fails — re-trip
    assert cb._state.value == "open"


def test_record_failure_in_half_open_does_not_use_original_opened_at():
    """Re-tripping from HALF_OPEN should stamp a new _opened_at."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()  # _opened_at = 100.0

    with patch("agent.circuit_breaker.time.monotonic", return_value=135.0):
        cb.check()  # -> HALF_OPEN
        cb.record_failure()  # re-trip; new _opened_at should be 135.0

    assert cb._opened_at == 135.0


def test_check_raises_after_re_trip_from_half_open():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=30.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        cb.record_failure()
    with patch("agent.circuit_breaker.time.monotonic", return_value=131.0):
        cb.check()  # -> HALF_OPEN
        cb.record_failure()  # re-trip to OPEN
        with pytest.raises(CircuitOpenError):
            cb.check()


# ── CircuitOpenError attributes and message ───────────────────────────────────


def test_circuit_open_error_is_llm_error():
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        err = CircuitOpenError(failures=3, recovery_at=130.0)
    assert isinstance(err, LLMError)


def test_circuit_open_error_stores_failures_attribute():
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        err = CircuitOpenError(failures=5, recovery_at=130.0)
    assert err.failures == 5


def test_circuit_open_error_stores_recovery_at_attribute():
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        err = CircuitOpenError(failures=3, recovery_at=130.0)
    assert err.recovery_at == 130.0


def test_circuit_open_error_message_contains_failure_count():
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        err = CircuitOpenError(failures=4, recovery_at=130.0)
    assert "4" in str(err)


def test_circuit_open_error_message_contains_recovery_seconds():
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        err = CircuitOpenError(failures=3, recovery_at=130.0)
    assert "30" in str(err)


def test_circuit_open_error_message_says_circuit_breaker_open():
    with patch("agent.circuit_breaker.time.monotonic", return_value=100.0):
        err = CircuitOpenError(failures=3, recovery_at=130.0)
    assert "Circuit breaker open" in str(err)


def test_circuit_open_error_clamps_remaining_to_zero_when_past_recovery():
    """remaining = max(0, ...) — past the deadline should show 0s."""
    with patch("agent.circuit_breaker.time.monotonic", return_value=200.0):
        err = CircuitOpenError(failures=1, recovery_at=100.0)
    assert "0s" in str(err)


def test_check_raises_circuit_open_error_with_correct_attributes():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=45.0)
    with patch("agent.circuit_breaker.time.monotonic", return_value=1000.0):
        cb.record_failure()
        cb.record_failure()
        with pytest.raises(CircuitOpenError) as exc_info:
            cb.check()
        assert exc_info.value.failures == 2
        assert exc_info.value.recovery_at == 1000.0 + 45.0
