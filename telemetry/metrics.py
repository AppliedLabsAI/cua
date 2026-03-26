"""Metric instrument definitions — counters, histograms, gauges.

All instruments are created lazily on first access so they respect the
global MeterProvider set by ``setup_telemetry()``.
"""

from __future__ import annotations

from functools import lru_cache

from telemetry.setup import get_meter


@lru_cache(maxsize=1)
def _meter():
    return get_meter("cua")


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def sessions_total():
    """Total CUA sessions created."""
    return _meter().create_counter(
        "cua.sessions.total",
        unit="1",
        description="Total CUA sessions",
    )


@lru_cache(maxsize=1)
def steps_total():
    """Total tool execution steps."""
    return _meter().create_counter(
        "cua.steps.total",
        unit="1",
        description="Total agent tool execution steps",
    )


@lru_cache(maxsize=1)
def guardrail_blocks_total():
    """Total guardrail blocks."""
    return _meter().create_counter(
        "cua.guardrail.blocks.total",
        unit="1",
        description="Total guardrail blocks",
    )


@lru_cache(maxsize=1)
def llm_calls_total():
    """Total LLM API calls."""
    return _meter().create_counter(
        "cua.llm.calls.total",
        unit="1",
        description="Total LLM API calls",
    )


@lru_cache(maxsize=1)
def errors_total():
    """Total errors across components."""
    return _meter().create_counter(
        "cua.errors.total",
        unit="1",
        description="Total errors across components",
    )


@lru_cache(maxsize=1)
def safety_degraded_total():
    """Total times safety fell back to deterministic degraded mode."""
    return _meter().create_counter(
        "cua.safety.degraded.total",
        unit="1",
        description="Total safety degraded-mode activations",
    )


# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def session_duration():
    """Session duration in milliseconds."""
    return _meter().create_histogram(
        "cua.session.duration",
        unit="ms",
        description="CUA session duration",
    )


@lru_cache(maxsize=1)
def iteration_duration():
    """Agent iteration duration in milliseconds."""
    return _meter().create_histogram(
        "cua.iteration.duration",
        unit="ms",
        description="Agent loop iteration duration",
    )


@lru_cache(maxsize=1)
def llm_call_duration():
    """LLM API call duration in milliseconds."""
    return _meter().create_histogram(
        "cua.llm.call.duration",
        unit="ms",
        description="LLM API call duration",
    )


@lru_cache(maxsize=1)
def llm_tokens_input():
    """LLM input token counts."""
    return _meter().create_histogram(
        "cua.llm.tokens.input",
        unit="tokens",
        description="LLM input tokens per call",
    )


@lru_cache(maxsize=1)
def llm_tokens_output():
    """LLM output token counts."""
    return _meter().create_histogram(
        "cua.llm.tokens.output",
        unit="tokens",
        description="LLM output tokens per call",
    )


@lru_cache(maxsize=1)
def tool_duration():
    """Tool execution duration in milliseconds."""
    return _meter().create_histogram(
        "cua.tool.duration",
        unit="ms",
        description="Tool execution duration",
    )


@lru_cache(maxsize=1)
def guardrail_check_duration():
    """Guardrail check duration in milliseconds."""
    return _meter().create_histogram(
        "cua.guardrail.check.duration",
        unit="ms",
        description="Guardrail check duration",
    )


# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def active_sessions():
    """Currently active sessions."""
    return _meter().create_up_down_counter(
        "cua.session.active",
        unit="1",
        description="Currently active CUA sessions",
    )
