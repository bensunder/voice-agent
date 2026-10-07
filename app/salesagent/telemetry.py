"""OpenTelemetry setup: traces + metrics for our code and Microsoft Agent Framework.

* With OTEL_EXPORTER_OTLP_ENDPOINT set, spans and metrics are exported over
  OTLP/HTTP (route through an OpenTelemetry Collector to Application Insights,
  Grafana, Jaeger, ...).
* Without it, providers are still installed so instrumentation is cheap no-op
  in-process; the cost/guardrail numbers are also persisted in Postgres and
  shown in the cockpit, so nothing operationally important depends on OTLP.

MAF emits workflow/executor spans and GenAI token metrics (`gen_ai.*`) once
instrumentation is enabled; our spans nest under them.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

log = logging.getLogger(__name__)
_configured = False

tracer = trace.get_tracer("salesagent")
meter = metrics.get_meter("salesagent")

# --- metrics (created against the global proxy; bound once providers exist) ---
llm_tokens = meter.create_counter("salesagent.llm.tokens", unit="{token}",
                                  description="Model tokens by agent, model and direction")
llm_cost = meter.create_counter("salesagent.llm.cost", unit="USD", description="Model spend")
llm_calls = meter.create_counter("salesagent.llm.calls", description="Model calls by status")
guardrail_hits = meter.create_counter("salesagent.guardrail.events", description="Guardrail interventions by stage/rule")
dispatch_routes = meter.create_counter("salesagent.campaign.dispatch", description="Dispatch decisions by route")
outcome_routes = meter.create_counter("salesagent.campaign.outcomes", description="Outcome routing by route")
workflow_duration = meter.create_histogram("salesagent.workflow.duration", unit="ms",
                                           description="MAF workflow run duration")


def configure(service_name: str, otlp_endpoint: str = "", environment: str = "production") -> None:
    """Install tracer/meter providers once per process and enable MAF instrumentation."""
    global _configured
    if _configured:
        return
    resource = Resource.create({"service.name": service_name, "deployment.environment": environment})
    tp = TracerProvider(resource=resource)
    readers = []
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        base = otlp_endpoint.rstrip("/")
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces")))
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
                                                     export_interval_millis=15000))
    trace.set_tracer_provider(tp)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    try:
        from agent_framework.observability import enable_instrumentation

        # Never export prompt/response content: caller words can contain personal data.
        enable_instrumentation(enable_sensitive_data=False)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the app
        log.warning("MAF instrumentation not enabled: %s", exc)
    _configured = True
    log.info("telemetry configured (otlp=%s)", bool(otlp_endpoint))


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else str(v))
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise
