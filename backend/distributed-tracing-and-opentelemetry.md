---
title: Distributed tracing and OpenTelemetry
section: backend
order: 69
phase: "Phase 9 · DevOps and observability"
summary: Spans, context propagation, W3C traceparent, head vs tail sampling with a runnable tracer, and OpenTelemetry's API, SDK, Collector and Django setup.
---

A checkout request takes 4 seconds. The API's metrics say it's slow; its logs say nothing is wrong. The time went somewhere in the payment service, a Postgres query, a retry against a card provider and a Kafka publish, across three services and two teams. Distributed tracing records each request's path as a tree of timed operations that crosses process boundaries, so you can see where the 4 seconds went and which hop failed. OpenTelemetry is the vendor-neutral standard for producing that data, plus metrics and logs, from your code.

## Distributed tracing

### Traces and spans

A **trace** is the full record of one request. It is a tree of **spans**; each span is one timed operation in one service.

| Span field | Meaning |
|---|---|
| Trace ID | 16 random bytes shared by every span in the trace |
| Span ID | 8 random bytes identifying this span |
| Parent span ID | The span that caused this one; empty for the root |
| Name | Low-cardinality operation name: `GET /orders/{id}`, `SELECT orders` |
| Kind | `SERVER`, `CLIENT`, `PRODUCER`, `CONSUMER` or `INTERNAL` |
| Start and end time | Duration is the difference |
| Attributes | Key-value details: route, status code, DB system, tenant |
| Events | Timestamped points inside the span, such as an exception |
| Status | `UNSET`, `OK` or `ERROR` |
| Links | References to spans in other traces (batch consumers, fan-in) |

```text
 trace 4bf92f35...                                  time ->
 orders    POST /checkout         [===========================] 412ms
 orders      SELECT orders          [==]                         18ms
 orders      HTTP POST payments        [==================]     260ms
 payments      POST /charges            [=================]     251ms
 payments        INSERT charge           [=]                     12ms
 payments        HTTP POST provider        [============]       190ms
 orders      publish order.paid                            [=]    6ms
```

Read the waterfall top-down: gaps between child spans are time spent in the parent's own code; a `CLIENT` span much longer than the matching `SERVER` span on the other side means network, connection pool or queueing delay; sequential spans that could be parallel are an optimization opportunity. Traces also give a service dependency map for free.

### Context propagation

For spans to join into one trace, the current trace ID and span ID must travel with the work:

- **In process**: the "current span" lives in a `contextvars.ContextVar` in Python (thread-local in older runtimes). New spans read it as their parent. `contextvars` follows asyncio tasks automatically, but not work handed to a raw thread pool; copy the context explicitly there (`contextvars.copy_context().run(...)`), or the span starts a new trace.
- **Across processes**: the caller **injects** the context into the outgoing carrier (HTTP headers, gRPC metadata, Kafka record headers, Celery task headers) and the receiver **extracts** it and uses it as the parent of its server or consumer span.
- **Async messaging**: the producer's span is the parent of the consumer's span, even if consumption happens an hour later. For batch consumers processing 500 messages at once, one consumer span with **links** to each producer context is more honest than picking one parent.
- **Baggage** (W3C Baggage header) carries application key-values (tenant ID, plan) to every downstream service. It is sent on every hop, visible to every service and to third parties you call, so keep it small and never put secrets or PII in it.

### W3C traceparent

The W3C Trace Context recommendation standardized the propagation headers so that tools from different vendors interoperate:

```http
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
tracestate: vendorname=opaque-value
```

| Part | Example | Meaning |
|---|---|---|
| Version | `00` | Format version |
| Trace ID | `4bf92f35...0e4736` | 32 hex characters (16 bytes); all zeros is invalid |
| Parent ID | `00f067aa0ba902b7` | 16 hex characters: the caller's span ID |
| Flags | `01` | Bit 0 = sampled: the caller is recording this trace |

`tracestate` carries vendor-specific data alongside. Older systems use Zipkin's B3 headers (`X-B3-TraceId` and friends) or vendor headers; propagators can read and write several formats during a migration. Treat incoming `traceparent` from the public internet with care: accepting a client's sampled flag lets anyone force 100% sampling, so edge services often start a new trace or ignore the flag, keeping the incoming ID only as a link.

### Sampling: head vs tail

Recording every span of every request is expensive at scale, so you keep a subset.

**Head-based sampling** decides at the root span, before anything is known about the request, and propagates the decision in the sampled flag; every downstream service honors it (a "parent-based" sampler). A ratio sampler hashes the trace ID, so a 10% decision is consistent everywhere. Cheap and simple, but it drops 90% of errors and slow requests along with the boring ones.

**Tail-based sampling** decides after the trace is complete. All spans are sent to a collector tier that buffers each trace for a decision window (say 10–30 seconds), then keeps it if any span errored, the root was slow, or it matches a rule, and samples a small percentage of the rest.

| | Head-based | Tail-based |
|---|---|---|
| Decision made | At the root, up front | After the trace completes |
| Keeps all errors and slow traces | No, only the sampled share | Yes, by policy |
| Cost in services | Low: unsampled spans aren't exported | All spans exported to collectors |
| Infrastructure | None | Stateful collector tier with memory for in-flight traces |
| Routing requirement | None | All spans of one trace must reach the same collector instance |
| Latency to see a trace | Immediate | Decision window |

Common production setup: parent-based head sampling at a high rate (or 100%) in services, a load-balancing collector layer that routes by trace ID, and tail sampling in a second collector layer. Metrics are computed from all requests before sampling, so dashboards stay accurate while traces are sampled. **Exemplars** attach a sampled trace ID to a histogram bucket, which is how you jump from a latency spike on a graph to a concrete trace.

### A minimal tracer

A runnable stdlib tracer showing spans, the `contextvars` parent chain, `traceparent` inject and extract across a simulated process boundary, deterministic head sampling, and a tail-sampling decision:

```python
import contextvars
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_current = contextvars.ContextVar("current_span", default=None)


@dataclass
class SpanContext:
    trace_id: str
    span_id: str
    sampled: bool


@dataclass
class Span:
    name: str
    service: str
    trace_id: str
    span_id: str
    parent_id: str | None
    sampled: bool
    start: float = field(default_factory=time.monotonic)
    end: float = 0.0
    attributes: dict = field(default_factory=dict)
    status: str = "UNSET"


def ratio_sampled(trace_id, ratio):
    """Deterministic head sampling: every service makes the same decision."""
    return int(trace_id[16:], 16) < ratio * 2**64


class Tracer:
    def __init__(self, service, ratio=1.0, exporter=None):
        self.service, self.ratio = service, ratio
        self.exported = exporter if exporter is not None else []

    @contextmanager
    def span(self, name, parent=None):
        parent = parent or _current.get()
        if parent is None:                          # root span: decide once
            trace_id = os.urandom(16).hex()
            parent_id, sampled = None, ratio_sampled(trace_id, self.ratio)
        else:                                       # child: inherit decision
            trace_id, parent_id, sampled = parent.trace_id, parent.span_id, parent.sampled
        span = Span(name, self.service, trace_id, os.urandom(8).hex(), parent_id, sampled)
        token = _current.set(span)
        try:
            yield span
        except Exception as exc:
            span.status = "ERROR"
            span.attributes["exception.type"] = type(exc).__name__
            raise
        finally:
            span.end = time.monotonic()
            _current.reset(token)
            if span.sampled:
                self.exported.append(span)


def inject(headers):
    span = _current.get()
    if span is not None:
        flags = "01" if span.sampled else "00"
        headers["traceparent"] = f"00-{span.trace_id}-{span.span_id}-{flags}"
    return headers


def extract(headers):
    m = TRACEPARENT.match(headers.get("traceparent", ""))
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None                                 # invalid: start a new trace
    return SpanContext(m.group(1), m.group(2), int(m.group(3), 16) & 1 == 1)


def tail_sample(spans, slow_s, keep_ratio):
    """Collector-side decision after the whole trace has arrived."""
    traces = {}
    for s in spans:
        traces.setdefault(s.trace_id, []).append(s)
    kept = set()
    for trace_id, group in traces.items():
        root = next(s for s in group if s.parent_id is None)
        if (any(s.status == "ERROR" for s in group)
                or root.end - root.start > slow_s
                or ratio_sampled(trace_id, keep_ratio)):
            kept.add(trace_id)
    return kept


if __name__ == "__main__":
    collector = []
    orders = Tracer("orders-api", exporter=collector)
    payments = Tracer("payments-api", exporter=collector)

    def payments_handler(headers):
        # runs in a fresh context, like a separate process: only headers cross
        with payments.span("POST /charges", parent=extract(headers)) as server:
            with payments.span("db.insert charge"):
                pass
            return server

    with orders.span("POST /checkout") as root:
        with orders.span("HTTP POST payments") as client:
            headers = inject({})
            server = contextvars.Context().run(payments_handler, headers)

    assert headers["traceparent"] == f"00-{root.trace_id}-{client.span_id}-01"
    assert server.trace_id == root.trace_id and server.parent_id == client.span_id
    assert len(collector) == 4 and len({s.trace_id for s in collector}) == 1
    assert {s.service for s in collector} == {"orders-api", "payments-api"}

    # head sampling at 0%: nothing recorded anywhere, flag 00 propagates
    dropped = []
    quiet_a = Tracer("orders-api", ratio=0.0, exporter=dropped)
    quiet_b = Tracer("payments-api", exporter=dropped)   # would sample on its own
    with quiet_a.span("GET /health-report"):
        h = inject({})
    assert h["traceparent"].endswith("-00")
    with quiet_b.span("GET /x", parent=extract(h)):
        pass
    assert dropped == []

    # invalid header (all-zero trace id) is ignored
    assert extract({"traceparent": "00-" + "0" * 32 + "-" + "1" * 16 + "-01"}) is None

    # deterministic ratio sampling
    assert ratio_sampled("a" * 16 + "0" * 16, 0.01)
    assert not ratio_sampled("a" * 16 + "f" * 16, 0.99)

    # tail sampling keeps errors and slow traces, drops boring fast ones
    spans = []
    t = Tracer("orders-api", exporter=spans)
    for i in range(50):
        try:
            with t.span("GET /orders") as s:
                if i == 7:
                    raise ValueError("boom")
                if i == 9:
                    s.start -= 2.0                  # pretend it took 2 s
        except ValueError:
            pass
    kept = tail_sample(spans, slow_s=1.0, keep_ratio=0.0)
    assert kept == {spans[7].trace_id, spans[9].trace_id}
    print("tracer ok")
```

The two details that real systems get wrong are visible here: the downstream service inherits the sampling decision instead of making its own (otherwise traces arrive with holes), and nothing crosses the process boundary except the header.

## OpenTelemetry

OpenTelemetry (OTel) is a CNCF project formed in 2019 by merging OpenTracing and OpenCensus. It defines a specification, language APIs and SDKs, a wire protocol (OTLP), semantic conventions, and the Collector, for traces, metrics and logs, with profiling being added. The point is decoupling: you instrument once, and the backend (Jaeger, Grafana Tempo, Zipkin, Honeycomb, Datadog, a cloud vendor) becomes a configuration choice.

### API, SDK and Collector

```text
 code and libraries        your process                    infrastructure
 +-------------------+   +----------------------------+   +-------------+
 | OTel API          |-->| OTel SDK                   |   | Collector   |
 | tracer.start_span |   |  sampler (parent-based)    |   |  receivers  |
 | meter.counter     |   |  span processor (batching) |-->|  processors |
 | (no-op w/o SDK)   |   |  resource: service.name    |   |  exporters  |
 +-------------------+   |  exporter: OTLP gRPC/HTTP  |   +------+------+
                         +----------------------------+          |
                                               Tempo, Jaeger, vendor ...
```

- **API**: the interfaces for creating spans, metrics and logs. Libraries depend only on the API; without an SDK installed every call is a cheap no-op, so a library can ship instrumentation without forcing a backend on its users.
- **SDK**: the implementation the application configures once at startup: a `TracerProvider` with a **Resource** (`service.name`, `service.version`, environment), a **sampler**, **span processors** (`BatchSpanProcessor` queues spans and exports in the background; `SimpleSpanProcessor` exports synchronously and is for tests), and **exporters**.
- **Collector**: a separate process that receives telemetry, processes it, and exports it anywhere. Pipelines are built from **receivers** (OTLP, Prometheus scrape, log files), **processors** (`memory_limiter`, `batch`, `k8sattributes` to add pod metadata, `attributes` or `transform` for redaction, `filter`, `tail_sampling`) and **exporters**. It runs as an **agent** (DaemonSet or sidecar, close to the app, cheap to reach) and as a **gateway** (a central, scalable tier for tail sampling, credentials and routing). Benefits: apps export to a local endpoint and never hold vendor keys, retries and buffering move out of the app, and switching or adding backends is a Collector config change.

### Auto-instrumentation for Django

The Python distro patches popular libraries at import time: Django (a `SERVER` span per request, named by route), psycopg and psycopg2 (a `CLIENT` span per query), `requests`, `httpx` and `urllib3` (outgoing spans plus `traceparent` injection), Redis, Celery (producer and consumer spans with context in task headers), and logging (adds trace and span IDs to log records). Illustrative setup:

```bash
pip install opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install     # installs instrumentations for detected libraries

export OTEL_SERVICE_NAME=orders-api
export OTEL_RESOURCE_ATTRIBUTES=service.version=1.42.0,deployment.environment.name=prod
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.2
export OTEL_PYTHON_LOG_CORRELATION=true
export DJANGO_SETTINGS_MODULE=config.settings

opentelemetry-instrument python manage.py runserver --noreload
```

Gunicorn's pre-fork model needs care: the SDK's background export thread and gRPC channel don't survive `fork()`, so configure the SDK in each worker after forking. The OpenTelemetry Python docs describe this pattern; an illustrative `gunicorn.conf.py`:

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased


def post_fork(server, worker):
    resource = Resource.create({"service.name": "orders-api", "service.version": "1.42.0"})
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(0.2)))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="otel-collector:4317", insecure=True)))
    trace.set_tracer_provider(provider)
    DjangoInstrumentor().instrument()
    PsycopgInstrumentor().instrument()
```

Celery workers fork too; instrument them in the `worker_process_init` signal. Then add manual spans where business logic matters, and record errors on them (illustrative):

```python
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("orders.checkout")


def capture_payment(order):
    with tracer.start_as_current_span("checkout.capture_payment") as span:
        span.set_attribute("app.tenant_id", order.tenant_id)
        span.set_attribute("app.order.amount_cents", order.amount_cents)
        try:
            return payments_client.capture(order)
        except PaymentDeclined as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, "declined"))
            raise
```

### Exporters

| Exporter | Use |
|---|---|
| OTLP over gRPC (port 4317) or HTTP/protobuf (port 4318) | The default; to a Collector or any backend that speaks OTLP |
| Prometheus | Exposes OTel metrics on a `/metrics` endpoint for scraping |
| Zipkin | Legacy Zipkin backends |
| Console | Local debugging |

Jaeger now ingests OTLP natively, and the old Jaeger-specific exporters in the SDKs have been deprecated in favor of OTLP. In practice: apps export OTLP to a nearby Collector, and only the Collector knows about backends.

### Semantic conventions

Semantic conventions standardize attribute names so dashboards, queries and backends work across languages and libraries. Use them instead of inventing `http_status` in one service and `statusCode` in another.

| Concept | Current attribute | Older name you'll still see |
|---|---|---|
| HTTP method | `http.request.method` | `http.method` |
| HTTP status | `http.response.status_code` | `http.status_code` |
| Route template | `http.route` | |
| Full URL | `url.full` | `http.url` |
| Database | `db.system.name` | `db.system` |
| Query text | `db.query.text` | `db.statement` |
| Messaging | `messaging.system`, `messaging.destination.name` | |
| Service | `service.name`, `service.version` | |
| Environment | `deployment.environment.name` | `deployment.environment` |

HTTP conventions were stabilized with those renames in 2023 and database conventions later; instrumentation libraries let you emit old, new or both names during migration. Span names must be low cardinality (`GET /orders/{id}`, never `GET /orders/81234`), and your own attributes go under a namespace such as `app.`.

## In practice

An illustrative Collector gateway: receive OTLP, protect itself from memory exhaustion, add Kubernetes metadata, drop a sensitive attribute, tail-sample (keep errors, slow traces and 5% of the rest), batch and export to Tempo. `tail_sampling` and `k8sattributes` are in the contrib distribution, and component names shift between Collector versions, so check against the version you run.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 20
  k8sattributes: {}
  attributes/scrub:
    actions:
      - key: http.request.header.authorization
        action: delete
  tail_sampling:
    decision_wait: 10s
    num_traces: 100000
    policies:
      - name: errors
        type: status_code
        status_code:
          status_codes: [ERROR]
      - name: slow
        type: latency
        latency:
          threshold_ms: 1000
      - name: baseline
        type: probabilistic
        probabilistic:
          sampling_percentage: 5
  batch: {}

exporters:
  otlp:
    endpoint: tempo.observability:4317
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, attributes/scrub, tail_sampling, batch]
      exporters: [otlp]
```

With more than one gateway replica, put a first Collector layer in front using the load-balancing exporter keyed on trace ID, so all spans of a trace reach the same tail sampler.

## Pitfalls

- **Broken context.** Symptom: traces end at a thread pool, an `asyncio` boundary started with a raw thread, a Celery task or a Kafka consumer, and a new trace starts on the other side. Fix: use instrumented clients, copy context into executors, propagate through message headers.
- **Each service samples independently.** Symptom: traces with missing middle spans. Fix: parent-based samplers everywhere; only the root decides.
- **High-cardinality span names.** Symptom: the backend's operation list explodes and aggregation is useless. Fix: route templates for names, IDs in attributes.
- **PII and secrets in attributes.** Query text with literals, headers, request bodies. Fix: sanitized query text, header allowlists, Collector redaction processors.
- **Tail sampling behind a plain load balancer.** Spans of one trace land on different collectors and decisions are made on fragments. Fix: trace-ID-aware load balancing.
- **SDK set up before Gunicorn forks.** Symptom: no spans from workers or a hung exporter. Fix: initialize in `post_fork`.
- **Synchronous export in production.** `SimpleSpanProcessor` adds export latency to every request. Fix: `BatchSpanProcessor`.
- **Clock skew.** Child spans appear to start before their parent across hosts. Fix: NTP everywhere; read cross-host gaps with skepticism.

## Interview questions

**What is a span and how do spans form a trace?** A span is a timed operation with a trace ID, its own span ID, a parent span ID, a name, kind, attributes and status. Spans sharing a trace ID form a tree through parent IDs, rooted at the span with no parent.

**How does trace context cross service boundaries?** The client injects the trace ID, its own span ID and the sampled flag into a carrier, the W3C `traceparent` header for HTTP or message headers for queues. The server extracts it and creates its span as a child of the client span.

**Head vs tail sampling?** Head sampling decides at the root and propagates the decision: cheap but blind to outcome. Tail sampling decides after the trace completes, so it can keep all errors and slow traces, but requires exporting every span to a stateful collector tier with trace-ID routing.

**Why use the OpenTelemetry Collector instead of exporting directly?** It decouples apps from backends, centralizes retries, batching, redaction, enrichment and tail sampling, keeps vendor credentials out of apps, and lets you change backends with config instead of redeploying every service.

**What is the difference between the OTel API and SDK?** Libraries depend on the API, which is a no-op until an SDK is installed. Applications configure the SDK: resource, sampler, processors and exporters. That split lets libraries ship instrumentation without choosing a backend.

**How would you add tracing to a Django and Celery system?** OTel distro with Django, psycopg, requests or httpx, Redis and Celery instrumentations; SDK initialized in Gunicorn's `post_fork` and Celery's `worker_process_init`; parent-based ratio sampling; OTLP to a local Collector; trace IDs injected into logs; manual spans around key business operations.

**How do traces relate to metrics and logs?** Metrics show that something is wrong, exemplars link a metric bucket to a sample trace, the trace shows where time or errors occurred, and the trace ID in log lines gives the detailed events for that span.

## Further reading

- *Dapper, a Large-Scale Distributed Systems Tracing Infrastructure*, Sigelman et al. (Google Technical Report, 2010).
- W3C Recommendation: Trace Context.
- OpenTelemetry documentation: Specification, Semantic Conventions, Collector, and the Python instrumentation guides.
- *Distributed Tracing in Practice*, Austin Parker, Daniel Spoonhower, Jonathan Mace, Ben Sigelman and Rebecca Isaacs (O'Reilly, 2020).
- *Observability Engineering*, Charity Majors, Liz Fong-Jones and George Miranda (O'Reilly, 2022).
