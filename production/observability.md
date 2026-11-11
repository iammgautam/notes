---
title: "Observability: logs, metrics, traces"
section: production
order: 1
summary: What logs, metrics and traces each answer, the RED and USE methods, OpenTelemetry basics, and why label cardinality decides your bill.
---

Monitoring tells you *that* something is wrong. Observability is whether you can work out *why* from the outside, without shipping new code to find out. In a multi-tenant Django service with background workers, a slow page can be a missing index, one noisy tenant, a saturated connection pool or a third-party API. You need three kinds of signal to tell those apart quickly.

## How it works

### The three signals

| Signal | Shape | Answers | Cost driver |
|---|---|---|---|
| Metrics | Numeric time series, pre-aggregated | "Is it broken? How much? Since when?" | Number of unique label combinations |
| Logs | Discrete events with fields | "What exactly happened to this request?" | Volume (bytes/day) |
| Traces | Tree of timed spans across services | "Where did the time go?" | Number of spans kept (sampling) |

- **Metrics** are cheap to store and fast to query because they are aggregated at write time. You lose the individual event. Use them for dashboards and alerts.
- **Logs** keep every detail of an event but are expensive to search at scale. Use them for the "what happened" once metrics told you "when".
- **Traces** link the work done for one request across processes: API → DB → Celery task → LLM provider. Each span has a start, a duration, attributes and a parent. Use them to answer "which hop is slow".

The useful glue is a shared ID. If every log line and every span carries the same `trace_id`, you can jump from a latency spike on a dashboard to an example trace to the exact log lines of that request.

### RED and USE

Two checklists that tell you which metrics to have before you need them.

**RED** (for request-driven services: APIs, workers consuming a queue):

- **Rate**: requests per second.
- **Errors**: failed requests per second (or ratio).
- **Duration**: latency distribution. Track p50, p95, p99, never just the average.

**USE** (for resources: CPU, memory, disk, connection pools, queues):

- **Utilization**: percent of time busy, or percent of capacity used.
- **Saturation**: work waiting (run queue, pool wait time, queue depth).
- **Errors**: resource-level errors (OOM kills, connection refused, disk errors).

RED tells you users are hurting. USE tells you which resource is the reason.

> **Rule of thumb:** Alert on RED (symptoms). Use USE on the dashboard you open after the alert fires.

### OpenTelemetry

OpenTelemetry (OTel) is the vendor-neutral standard for producing telemetry. The pieces:

- **API/SDK**: libraries in your process that create spans and metrics.
- **Instrumentation**: auto-instrumentation packages that wrap Django, psycopg, requests, Celery, Redis, so you get spans without touching business code.
- **Exporter / OTLP**: the wire protocol to ship data out.
- **Collector**: a separate process that receives, batches, samples, filters and forwards to one or more backends (Jaeger, Tempo, Honeycomb, Datadog, Grafana Cloud).

Context propagation is the key mechanism. The incoming `traceparent` HTTP header carries the trace ID and parent span ID. Instrumented HTTP clients inject it on outgoing calls; the Celery instrumentation puts it in task headers, so a task's spans join the trace of the request that enqueued it.

## In practice

### Minimal Django setup

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp \
  opentelemetry-instrumentation-django \
  opentelemetry-instrumentation-psycopg \
  opentelemetry-instrumentation-celery
```

```bash
export OTEL_SERVICE_NAME=billing-api
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1   # keep 10% of traces
opentelemetry-instrument gunicorn app.wsgi:application -w 4
```

Add business context to the current span so you can filter traces by tenant:

```python
from opentelemetry import trace

class TenantSpanMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        span = trace.get_current_span()
        tenant = getattr(request, "tenant", None)
        if tenant:
            span.set_attribute("tenant.id", str(tenant.id))
        return self.get_response(request)
```

Manual spans for work auto-instrumentation can't see:

```python
tracer = trace.get_tracer(__name__)

def build_report(report_id):
    with tracer.start_as_current_span("report.build") as span:
        span.set_attribute("report.id", report_id)
        rows = fetch_rows(report_id)
        span.set_attribute("report.rows", len(rows))
        return render(rows)
```

### Metrics with Prometheus

```python
from prometheus_client import Counter, Histogram

TASKS = Counter("tasks_total", "Tasks processed", ["queue", "status"])
TASK_SECONDS = Histogram(
    "task_duration_seconds", "Task runtime", ["queue"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

def run(task):
    with TASK_SECONDS.labels(task.queue).time():
        ok = task.execute()
    TASKS.labels(task.queue, "ok" if ok else "error").inc()
```

A RED query in PromQL for p95 latency per route:

```text
histogram_quantile(0.95,
  sum by (le, route) (rate(http_request_duration_seconds_bucket[5m])))
```

### Cardinality

Cardinality is the number of distinct time series a metric produces: the product of distinct values of each label.

```text
http_requests_total{route, method, status}
  60 routes x 5 methods x 8 statuses = 2,400 series      fine

http_requests_total{route, method, status, tenant_id}
  2,400 x 3,000 tenants             = 7,200,000 series   outage
```

Each series costs memory in Prometheus and money in a hosted backend. Rules:

- Never put user IDs, request IDs, raw URLs, emails or error messages in metric labels.
- Use the route template (`/api/invoices/<id>/`), not the path (`/api/invoices/8812/`).
- High-cardinality data belongs in **logs and trace attributes**, where it is cheap per event. `tenant.id` on a span is fine; `tenant_id` as a metric label usually is not.
- If you need per-tenant metrics, limit to a top-N or a tier label (`plan=enterprise`).

## Pitfalls

- **Averages hide pain.** A 200 ms mean can hide a p99 of 8 s that one large tenant hits every time. Use histograms.
- **Unsampled tracing at scale.** 100% of traces on a busy API costs more than the app. Head-sample at 1–10%, and use tail sampling in the Collector to always keep errors and slow traces.
- **Broken propagation.** Traces that stop at the queue boundary usually mean the Celery or custom task executor isn't injecting/extracting context. For a homegrown Postgres task queue, store the `traceparent` in the task row and restore it when the worker picks it up.
- **Logs as metrics.** Counting log lines to build a dashboard works until log volume is sampled or dropped. Emit a real counter.
- **Dashboards nobody reads.** Ten panels per service with no RED summary at the top. Start every dashboard with rate, errors, p95/p99.
- **Clock skew** makes child spans appear before parents across hosts. Use NTP; don't chase phantom negative durations.

## Rules of thumb

- Metrics to detect, traces to localize, logs to explain.
- Every service exposes RED; every shared resource (DB pool, queue, worker pool) exposes USE.
- One correlation key (`trace_id`) present in logs, spans and Sentry events.
- Keep metric label values bounded; put the unbounded stuff on spans and logs.
- Instrument the queue: depth, age of oldest item, task duration, retries.

## Interview angle

- **"Logs vs metrics vs traces?"** Metrics are aggregated numbers for detection and alerting; logs are per-event detail for explanation; traces show the causal path and timing across services. You correlate them with a shared trace ID.
- **"What is cardinality and why does it matter?"** The number of unique label combinations per metric. It multiplies, drives memory and cost, and unbounded labels like user ID can take down a metrics backend.
- **"How do you trace through an async job?"** Propagate the W3C trace context in the job payload/headers and continue the trace in the worker as a child or linked span.

Related: [Structured logging]({{ '/production/structured-logging/' | relative_url }}), [SLOs and alerting]({{ '/production/slos-and-alerting/' | relative_url }}).
