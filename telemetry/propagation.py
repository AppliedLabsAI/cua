"""Cross-process trace context propagation via W3C Traceparent.

The outer API injects a ``TRACEPARENT`` env var into the Modal sandbox.
The inner agent extracts it so both processes share one trace ID.
"""

from __future__ import annotations

from opentelemetry import trace as otel_trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()


def inject_trace_context() -> dict[str, str]:
    """Capture the current span's trace context as a dict of headers.

    Returns e.g. ``{"traceparent": "00-<trace_id>-<span_id>-01"}``.
    The caller should merge these into the sandbox environment variables.
    """
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier


def extract_trace_context(traceparent: str, tracestate: str = "") -> Context:
    """Reconstruct a Context from a W3C traceparent (+ optional tracestate).

    Use the returned context as the parent when starting the inner agent's
    root span::

        ctx = extract_trace_context(os.environ["TRACEPARENT"])
        with tracer.start_as_current_span("cua.agent.run", context=ctx):
            ...
    """
    carrier: dict[str, str] = {}
    if traceparent:
        carrier["traceparent"] = traceparent
    if tracestate:
        carrier["tracestate"] = tracestate
    return _propagator.extract(carrier)


def current_trace_id() -> str:
    """Return the current trace ID as a hex string, or empty if none."""
    span = otel_trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return ""
