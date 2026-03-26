"""Tests for the telemetry package.

Verifies span hierarchy, attribute constants, context propagation,
no-op mode, and metric instrument creation.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)


class _InMemoryExporter(SpanExporter):
    """Minimal in-memory span exporter for tests."""

    def __init__(self):
        self._spans: list = []
        self._lock = threading.Lock()

    def export(self, spans: Sequence) -> SpanExportResult:
        with self._lock:
            self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def get_finished_spans(self) -> list:
        with self._lock:
            return list(self._spans)

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _reset_otel_provider():
    """Reset OTel global TracerProvider so tests can set their own."""
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001
    otel_trace._TRACER_PROVIDER = None  # noqa: SLF001


@pytest.fixture
def otel_exporter():
    """Set up an in-memory exporter for capturing spans in tests."""
    _reset_otel_provider()
    exporter = _InMemoryExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace.set_tracer_provider(provider)
    yield exporter
    exporter.shutdown()
    _reset_otel_provider()


# ---------------------------------------------------------------------------
# Span constants
# ---------------------------------------------------------------------------


def test_span_names_are_strings():
    """All span name constants should be non-empty strings."""
    from telemetry import spans

    span_names = [
        spans.SESSION,
        spans.SANDBOX_CREATE,
        spans.AGENT_RUN,
        spans.AGENT_SETUP,
        spans.BROWSER_LAUNCH,
        spans.BLINDERS_EXTRACT,
        spans.AGENT_ITERATION,
        spans.CONTEXT_PRUNE,
        spans.LLM_CALL,
        spans.TOOL_EXECUTE,
        spans.GUARDRAIL_CHECK,
        spans.GUARDRAIL_LLM,
        spans.BROWSER_ACTION,
        spans.CAPTCHA_HANDLE,
    ]
    for name in span_names:
        assert isinstance(name, str)
        assert name.startswith("cua.")


def test_attribute_keys_are_strings():
    """All attribute key constants should be non-empty strings."""
    from telemetry import spans

    attr_keys = [
        spans.ATTR_SESSION_ID,
        spans.ATTR_DIRECTIVE,
        spans.ATTR_MODEL,
        spans.ATTR_TOOL_ACTION,
        spans.ATTR_TOOL_STEP,
        spans.ATTR_GUARD_ALLOWED,
        spans.ATTR_GENAI_SYSTEM,
        spans.ATTR_GENAI_INPUT_TOKENS,
        spans.ATTR_BROWSER_ACTION,
    ]
    for key in attr_keys:
        assert isinstance(key, str)
        assert len(key) > 0


# ---------------------------------------------------------------------------
# No-op mode (CUA_OTEL_ENABLED not set)
# ---------------------------------------------------------------------------


def test_setup_noop_when_disabled(monkeypatch):
    """setup_telemetry should be a no-op when OTEL_SDK_DISABLED is true."""
    import telemetry.setup as setup_mod

    setup_mod._initialized = False

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    setup_mod.setup_telemetry("test-noop")

    assert setup_mod._initialized is False


def test_get_tracer_returns_tracer():
    """get_tracer should return a valid tracer even when not initialized."""
    from telemetry.setup import get_tracer

    tracer = get_tracer("test")
    assert tracer is not None
    # Should be able to create spans (no-op spans)
    with tracer.start_as_current_span("test.span") as span:
        assert span is not None


def test_get_meter_returns_meter():
    """get_meter should return a valid meter even when not initialized."""
    from telemetry.setup import get_meter

    meter = get_meter("test")
    assert meter is not None


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------


def test_inject_returns_empty_without_active_span():
    """inject_trace_context should return empty dict when no span is active."""
    from telemetry.propagation import inject_trace_context

    result = inject_trace_context()
    assert isinstance(result, dict)
    # No active span → no traceparent
    assert "TRACEPARENT" not in result or result.get("TRACEPARENT") == ""


def test_inject_extract_roundtrip(otel_exporter):
    """Injected trace context should be extractable and preserve trace ID."""
    from telemetry.propagation import (
        extract_trace_context,
        inject_trace_context,
    )

    tracer = otel_trace.get_tracer("test")

    with tracer.start_as_current_span("parent.span") as parent:
        parent_ctx = parent.get_span_context()
        parent_trace_id = format(parent_ctx.trace_id, "032x")

        # Inject current context
        carrier = inject_trace_context()
        assert "TRACEPARENT" in carrier
        assert parent_trace_id in carrier["TRACEPARENT"]

    # Extract in a "different process"
    extracted_ctx = extract_trace_context(carrier["TRACEPARENT"])
    assert extracted_ctx is not None

    # Create a child span using extracted context
    with tracer.start_as_current_span("child.span", context=extracted_ctx) as child:
        child_ctx = child.get_span_context()
        child_trace_id = format(child_ctx.trace_id, "032x")
        # Same trace ID — proves context was propagated
        assert child_trace_id == parent_trace_id


def test_extract_empty_traceparent():
    """Extracting empty traceparent should return a valid (root) context."""
    from telemetry.propagation import extract_trace_context

    ctx = extract_trace_context("")
    assert ctx is not None


def test_current_trace_id_empty_without_span():
    """current_trace_id should return empty string when no span is active."""
    from telemetry.propagation import current_trace_id

    # Reset to no-op provider to ensure no active span
    otel_trace.set_tracer_provider(otel_trace.NoOpTracerProvider())
    result = current_trace_id()
    assert result == "" or result == "0" * 32


# ---------------------------------------------------------------------------
# Span hierarchy
# ---------------------------------------------------------------------------


def test_span_hierarchy_nesting(otel_exporter):
    """Verify that child spans correctly reference parent spans."""
    from telemetry.spans import AGENT_ITERATION, AGENT_RUN, LLM_CALL, TOOL_EXECUTE

    tracer = otel_trace.get_tracer("test")

    with tracer.start_as_current_span(AGENT_RUN) as run_span:  # noqa: SIM117
        with tracer.start_as_current_span(AGENT_ITERATION) as iter_span:
            with tracer.start_as_current_span(LLM_CALL):
                pass
            with tracer.start_as_current_span(TOOL_EXECUTE):
                pass

    spans = otel_exporter.get_finished_spans()
    span_map = {s.name: s for s in spans}

    assert AGENT_RUN in span_map
    assert AGENT_ITERATION in span_map
    assert LLM_CALL in span_map
    assert TOOL_EXECUTE in span_map

    # Iteration should be child of run
    assert (
        span_map[AGENT_ITERATION].parent.span_id == run_span.get_span_context().span_id
    )
    # LLM and tool should be children of iteration
    assert span_map[LLM_CALL].parent.span_id == iter_span.get_span_context().span_id
    assert span_map[TOOL_EXECUTE].parent.span_id == iter_span.get_span_context().span_id


def test_span_attributes(otel_exporter):
    """Verify attributes are correctly set on spans."""
    from telemetry.spans import ATTR_TOOL_ACTION, ATTR_TOOL_STEP, TOOL_EXECUTE

    tracer = otel_trace.get_tracer("test")

    with tracer.start_as_current_span(
        TOOL_EXECUTE,
        attributes={
            ATTR_TOOL_ACTION: "click",
            ATTR_TOOL_STEP: 5,
        },
    ):
        pass

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    tool_span = spans[0]
    assert tool_span.attributes[ATTR_TOOL_ACTION] == "click"
    assert tool_span.attributes[ATTR_TOOL_STEP] == 5


def test_span_events(otel_exporter):
    """Verify events can be added to spans."""
    from telemetry.spans import AGENT_ITERATION, EVENT_STUCK

    tracer = otel_trace.get_tracer("test")

    with tracer.start_as_current_span(AGENT_ITERATION) as span:
        span.add_event(EVENT_STUCK, attributes={"hint": "Try a different approach"})

    spans = otel_exporter.get_finished_spans()
    assert len(spans) == 1
    events = spans[0].events
    assert len(events) == 1
    assert events[0].name == EVENT_STUCK
    assert events[0].attributes["hint"] == "Try a different approach"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metric_instruments_creation():
    """Verify all metric instruments can be created without error."""
    from telemetry.metrics import (
        active_sessions,
        errors_total,
        guardrail_blocks_total,
        guardrail_check_duration,
        iteration_duration,
        llm_call_duration,
        llm_calls_total,
        llm_tokens_input,
        llm_tokens_output,
        session_duration,
        sessions_total,
        steps_total,
        tool_duration,
    )

    # Just verify they can be called without raising
    assert sessions_total() is not None
    assert steps_total() is not None
    assert guardrail_blocks_total() is not None
    assert llm_calls_total() is not None
    assert errors_total() is not None
    assert session_duration() is not None
    assert iteration_duration() is not None
    assert llm_call_duration() is not None
    assert llm_tokens_input() is not None
    assert llm_tokens_output() is not None
    assert tool_duration() is not None
    assert guardrail_check_duration() is not None
    assert active_sessions() is not None


def test_metrics_record_without_error():
    """Verify metrics can record values without raising (no-op mode)."""
    from telemetry.metrics import (
        active_sessions,
        llm_call_duration,
        sessions_total,
        steps_total,
    )

    # These should all succeed even in no-op mode
    sessions_total().add(1, {"status": "success"})
    steps_total().add(1, {"action": "click", "success": "True"})
    llm_call_duration().record(150, {"model": "test", "streaming": "True"})
    active_sessions().add(1)
    active_sessions().add(-1)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def test_instrument_fastapi_noop_when_disabled(monkeypatch):
    """instrument_fastapi should be a no-op when OTEL_SDK_DISABLED is true."""
    from fastapi import FastAPI

    from telemetry.middleware import instrument_fastapi

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    app = FastAPI()
    instrument_fastapi(app)
