"""FastAPI middleware for automatic HTTP span instrumentation.

Uses the official OpenTelemetry FastAPI instrumentor when OTel is enabled.
"""

from __future__ import annotations

from fastapi import FastAPI


def instrument_fastapi(app: FastAPI) -> None:
    """Attach OTel instrumentation to a FastAPI app.

    No-op when ``cua_otel_enabled`` is False.
    """
    from settings import get_settings

    if get_settings().otel_sdk_disabled:
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
