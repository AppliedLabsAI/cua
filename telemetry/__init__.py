"""OpenTelemetry instrumentation for the CUA agent.

Provides distributed tracing and metrics across the outer API and inner
sandbox agent. Controlled by ``CUA_OTEL_ENABLED`` — when disabled, all
calls return no-op instances with zero overhead.
"""

from telemetry.helpers import (
    execute_tool_with_span,
    finalize_llm_span,
    llm_span_attrs,
    record_text_block,
    record_thinking_block,
)
from telemetry.setup import get_meter, get_tracer, setup_telemetry

__all__ = [
    "execute_tool_with_span",
    "finalize_llm_span",
    "get_meter",
    "get_tracer",
    "llm_span_attrs",
    "record_text_block",
    "record_thinking_block",
    "setup_telemetry",
]
