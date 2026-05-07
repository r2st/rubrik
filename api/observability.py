"""Optional observability: Prometheus metrics, OpenTelemetry tracing, Sentry.

Each integration is opt-in via Settings. No-op if not configured.

- **Metrics**:  `/metrics` Prometheus endpoint with request rate, latency
                histograms, status codes per route. Always-on by default.
- **Tracing**:  OpenTelemetry FastAPI instrumentation. Exports OTLP/HTTP if
                `settings.otel_endpoint` is set; otherwise spans go nowhere.
- **Sentry**:   Forwards unhandled exceptions if `settings.sentry_dsn` is set.

Backends are vendor-neutral — Grafana/Tempo/Datadog/Honeycomb all accept the
same wire formats.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.logging_config import get_logger
from src.settings import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

log = get_logger(__name__)


def install_metrics(app: FastAPI, settings: Settings) -> None:
    """Mount /metrics if metrics are enabled.

    The toggle is a *runtime* setting (admin-tunable). On import we just
    consult the runtime store; if it's disabled, the endpoint isn't mounted
    until next process start. (Metrics need to be wired up before requests
    flow, so true hot-toggling would require a restart.)
    """
    from src.settings import get_runtime_view
    if not get_runtime_view().metrics_enabled:
        log.info("Prometheus metrics disabled by runtime config")
        return
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        log.warning("prometheus-fastapi-instrumentator not installed — metrics disabled")
        return

    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        # Don't blow up cardinality on the meeting-detail path
        excluded_handlers=["/metrics", "/api/health", "/favicon.ico", "/favicon.svg"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
    log.info("Prometheus metrics mounted at /metrics")


def install_tracing(app: FastAPI, settings: Settings) -> None:
    """Wire up OpenTelemetry instrumentation. No-op if no exporter endpoint."""
    if not settings.otel_endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("opentelemetry packages not installed — tracing disabled")
        return

    provider = TracerProvider(
        resource=Resource.create({
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.env,
        })
    )
    exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    log.info("OpenTelemetry tracing exporting to %s", settings.otel_endpoint)


def install_sentry(settings: Settings) -> None:
    """Initialize Sentry if a DSN is configured. Must be called before app start."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        log.warning("sentry-sdk not installed — error tracking disabled")
        return

    from src.settings import get_runtime_view
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        traces_sample_rate=get_runtime_view().sentry_traces_sample_rate,
        integrations=[StarletteIntegration()],
        send_default_pii=False,
    )
    log.info("Sentry initialized (env=%s)", settings.env)
