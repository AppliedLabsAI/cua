"""TracerProvider and MeterProvider bootstrap.

Call ``setup_telemetry(service_name)`` once at process startup.
When ``OTEL_SDK_DISABLED`` is True (default), returns no-op providers
so instrumentation calls have zero overhead.
"""

from __future__ import annotations

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace

_initialized = False


def setup_telemetry(service_name: str) -> None:
    """Initialize OTel TracerProvider + MeterProvider with OTLP exporter.

    Safe to call multiple times — only the first call takes effect.
    When ``OTEL_SDK_DISABLED`` is True, does nothing (no-op providers
    are already the default).
    """
    global _initialized
    if _initialized:
        return

    from settings import get_settings

    settings = get_settings()
    if settings.otel_sdk_disabled:
        return
    _initialized = True

    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": settings.otel_resource_env,
        }
    )

    sampler = TraceIdRatioBased(settings.otel_traces_sampler_arg)
    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                insecure=settings.otel_exporter_otlp_insecure,
            )
        )
    )
    otel_trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=settings.otel_exporter_otlp_insecure,
        ),
        export_interval_millis=10_000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    otel_metrics.set_meter_provider(meter_provider)


def get_tracer(name: str = "cua") -> otel_trace.Tracer:
    """Return a tracer from the global TracerProvider."""
    return otel_trace.get_tracer(name)


def get_meter(name: str = "cua") -> otel_metrics.Meter:
    """Return a meter from the global MeterProvider."""
    return otel_metrics.get_meter(name)
