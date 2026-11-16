---
title: Centralized logging and metrics
section: backend
order: 68
phase: "Phase 9 · DevOps and observability"
summary: Structured logs with correlation IDs, log pipelines into Loki or ELK, retention and PII; metric types, Prometheus pulls, cardinality, PromQL and dashboards.
---

With thirty pods across three services, `ssh` and `grep` stop working: the pod that logged the error was replaced an hour ago, and the request touched four services. Logs and metrics answer different questions. **Metrics** are cheap numeric aggregates, the right tool for dashboards and alerts ("error rate is 4%, p99 is 2 s"). **Logs** are detailed records of individual events, the right tool for "what happened to this request, for this tenant". **Traces** connect one request's path across services and are covered in [distributed tracing and OpenTelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}). A good setup links all three: an alert fires on a metric, a dashboard narrows it to a route, an exemplar or trace ID leads to the logs of one failing request.

## Centralized logging

### Structured logs

Write logs as one JSON object per line, with stable field names, instead of free text.

```json
{"ts": "2026-11-16T10:42:07Z", "level": "ERROR", "event": "payment capture failed",
 "service": "orders-api", "version": "1.42.0", "env": "prod",
 "request_id": "9f2c1e7a", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
 "tenant_id": "acme", "order_id": 81234, "provider": "stripe",
 "error_code": "card_declined", "duration_ms": 812}
```

- **Machine-queryable.** `tenant_id = "acme" AND level = "ERROR"` is a filter, not a regex over prose.
- **Stable event names.** `"payment capture failed"` with variables in separate fields, not `f"payment {order_id} failed for {tenant}"`. Constant messages group cleanly and don't leak data into an unindexed text blob.
- **Standard context on every line**: service, version, environment, request ID, trace ID, tenant. Add these once in a formatter or middleware, not at every call site.
- **Levels with meaning.** `ERROR` means someone should look; `WARNING` means degraded but handled; `INFO` for business events and request summaries; `DEBUG` off in production or sampled.
- **Write to stdout.** In containers the runtime captures stdout and stderr to files on the node; a node agent ships them. Applications shouldn't manage log files or ship logs themselves.

In Django, `structlog` or `python-json-logger` produce JSON; the stdlib is enough, as the runnable example under "In practice" shows.

### Correlation IDs

A correlation (request) ID ties together every log line produced while handling one request, across services and async work.

1. The edge (load balancer, API gateway or first service) accepts an incoming `X-Request-ID` from trusted sources or generates one.
2. Middleware stores it in a `contextvars.ContextVar`, which is per-thread and per-asyncio-task, so concurrent requests don't mix.
3. The log formatter adds it to every line automatically.
4. Outgoing HTTP calls forward it as a header; Celery tasks and Kafka messages carry it in message headers, and the consumer binds it before running the task.
5. The response returns it, so a customer support ticket can quote it.

When tracing is enabled, the W3C trace ID is the better correlation ID: it already propagates everywhere and links logs to the trace waterfall. Many teams log both.

### Log pipelines

```text
  each node                                      storage and query
 +-----------------------------------+
 | app pods -> stdout / stderr       |
 |   container runtime writes        |
 |   /var/log/containers/*.log       |
 |                                   |
 | agent (DaemonSet): Fluent Bit,    |     +--------------------------+
 | Vector or OTel Collector          |---->| Loki: labels indexed,    |
 |  - tail and parse JSON            |  |  | chunks in object storage |
 |  - add pod, namespace, app labels |  |  +--------------------------+
 |  - drop health checks, redact     |  |  +--------------------------+
 |  - buffer and retry               |  +->| Elasticsearch/OpenSearch:|
 +-----------------------------------+     | full-text inverted index |
          (Kafka buffer at very            +--------------------------+
           high volume, optional)               query: Grafana, Kibana
```

The agent runs once per node (a DaemonSet), tails container log files, enriches each record with Kubernetes metadata, filters and redacts, buffers to disk when the backend is slow, and ships in batches. At very high volume, a Kafka topic between agents and storage absorbs bursts and lets several consumers (search, archive to S3, security analytics) read the same stream.

| | Loki | Elasticsearch / OpenSearch | Managed (CloudWatch Logs, Datadog, etc.) |
|---|---|---|---|
| Index | Only labels (namespace, app, level); lines stored compressed in object storage | Every field plus full-text | Varies |
| Query | LogQL: pick streams by label, then filter and parse lines | Arbitrary full-text search and aggregations | Vendor query language |
| Cost | Low storage, small index | High: RAM, SSD and shard management | Per GB ingested and retained |
| Weak spot | Searching for a needle without narrowing labels scans a lot; high-cardinality labels break it | Cost and operational load at TB/day | Bills grow with volume; lock-in |

Loki fits "find logs for this service and time range, then grep"; Elasticsearch fits ad hoc search across everything. Never put request IDs or user IDs into Loki labels; they belong in the log line.

### Retention and cost

Logs are usually the largest observability bill. Do the arithmetic early: 2,000 requests per second, 5 lines per request, 500 bytes per line is 5 MB/s, or about 432 GB per day and 13 TB per 30 days before compression.

- **Tier by age.** Hot and searchable for 7–14 days, then cheaper storage (object storage, frozen tiers) for 30–90 days, then delete or archive.
- **Tier by type.** Application logs: weeks. Security and audit logs: often a year or more, in an append-only store with restricted access. Compliance regimes may set minimums; privacy law sets maximums for personal data.
- **Cut volume at the source.** Drop health check and readiness probe logs, sample successful-request `INFO` logs at high volume while keeping all errors, rate-limit log storms (a tight loop logging the same exception 10,000 times a second).
- **Don't use logs as metrics.** Counting requests by querying logs is expensive and slow. Emit a counter.

### PII and sensitive data

Logs get copied into many systems, kept for months, and read by many people, so they are a common source of data leaks and privacy violations.

- **Never log**: passwords, session tokens, API keys, `Authorization` headers, full card numbers (PCI DSS forbids it), or full request bodies of auth and payment endpoints.
- **Minimize personal data**: log a user or tenant ID, not name, email and address. If you must correlate on an email, log a keyed hash (HMAC with a secret key), since a plain hash of an email can be reversed by dictionary.
- **Redact in two places**: at the source in a logging filter or formatter (the primary control), and in the pipeline as a backstop (regex for card numbers, emails, JWTs).
- **Right to erasure**: under GDPR you may have to delete a person's data, which is impractical across immutable log archives. Keeping PII out of logs is far cheaper than finding it later.
- **Access control and audit** on the log store itself, with separate retention for audit logs.

## Metrics and monitoring

### Counters, gauges and histograms

| Type | What it is | Example | How to query |
|---|---|---|---|
| Counter | Monotonically increasing total, resets to 0 on restart | `http_requests_total` | Always wrap in `rate()` or `increase()` |
| Gauge | Value that goes up and down | `celery_queue_length`, `db_pool_in_use` | Use directly, `avg_over_time`, `max` |
| Histogram | Counts of observations per bucket, plus `_sum` and `_count` | `http_request_duration_seconds_bucket{le="0.25"}` | `histogram_quantile()` over `rate()` of buckets |
| Summary | Quantiles computed in the client | `rpc_duration_seconds{quantile="0.99"}` | Read as is; **can't be aggregated** across instances |

Histograms are the right default for latency: buckets from many pods can be summed and a percentile computed over the total, while averaging per-pod p99s from summaries is mathematically meaningless. The price is resolution: `histogram_quantile` interpolates linearly inside a bucket, so the answer is only as precise as the bucket boundaries around it. Choose buckets around your SLO threshold (if the objective is 300 ms, have a bucket boundary at 0.3). Newer Prometheus versions support native histograms with automatic exponential buckets, which remove most of the bucket-tuning problem.

### The Prometheus pull model

Prometheus **scrapes**: every 15–60 seconds it sends `GET /metrics` to each target and stores the samples in its local time-series database. Targets come from service discovery (the Kubernetes API, Consul, cloud APIs), filtered and relabeled by config.

Why pull:

- **Liveness for free.** A target that can't be scraped gets `up == 0`, a direct signal that it's down or unreachable.
- **Central control** of scrape frequency and of what is collected; applications just expose current values.
- **Simple clients.** An instrumented process holds counters in memory and renders text on request.

Limits and workarounds: short-lived batch jobs may end before a scrape (push their final values to a Pushgateway, or better, emit them as part of a long-running service's metrics); targets behind NAT or in other networks need an agent close to them. A single Prometheus keeps local data for weeks and doesn't replicate it, so long-term and global views come from remote-write storage such as Thanos, Grafana Mimir, Cortex or VictoriaMetrics. OpenTelemetry metrics can be pushed over OTLP and land in the same backends.

Python-specific: Gunicorn runs several worker processes and each scrape hits one of them, so counts jump around. Use `prometheus_client` multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`) or django-prometheus, which aggregate across workers.

### Cardinality

Every unique combination of metric name and label values is a separate time series with its own memory and storage. Cardinality is the product of label value counts:

```text
http_request_duration_seconds_bucket
  method (4) x route (50) x status (10) x pod (30) x buckets (12)
  = 720,000 series                              -> fine
  ... x user_id (100,000)                       -> 72 billion: outage
```

- **Never use unbounded values as labels**: user IDs, request IDs, emails, raw URL paths (`/orders/81234`), error messages. Use the route template (`/orders/<int:pk>`), and put IDs in logs and traces.
- **Watch the multipliers**: histogram buckets multiply everything; `pod` labels multiply by replica count and churn on every deploy.
- **Tenant as a label** is fine for tens or hundreds of tenants, dangerous for tens of thousands. Aggregate per tenant in a recording rule only for the top tenants, or move per-tenant analytics to a warehouse.
- **Monitor it**: active series per metric (`topk` on `count by (__name__)`), and drop or rewrite offending labels with `metric_relabel_configs`. Each active series costs on the order of kilobytes of memory in Prometheus, so a few million series means many GB of RAM.

### PromQL basics

```text
# requests per second by route, averaged over the last 5 minutes
sum by (route) (rate(http_requests_total{job="orders-api"}[5m]))

# error ratio: 5xx over all
sum(rate(http_requests_total{job="orders-api",code=~"5.."}[5m]))
  / sum(rate(http_requests_total{job="orders-api"}[5m]))

# p99 latency per route: sum buckets across pods first, keep le
histogram_quantile(0.99,
  sum by (le, route) (
    rate(http_request_duration_seconds_bucket{job="orders-api"}[5m])))

# saturation: queue depth and connection pool use
max by (queue) (celery_queue_length)
max(db_pool_in_use / db_pool_size)

# compared with the same time last week
sum(rate(http_requests_total[5m]))
  / sum(rate(http_requests_total[5m] offset 1w))

# which metrics have the most series
topk(10, count by (__name__) ({__name__=~".+"}))
```

Rules that prevent wrong answers: apply `rate()` before `sum()` (rate handles counter resets per series; summing first hides them); never `rate()` a gauge; keep `le` in the `by` clause for `histogram_quantile`; use a range at least four times the scrape interval. Expensive queries used by dashboards and alerts belong in **recording rules** that precompute them.

### Dashboards

Three standard lenses:

- **RED** for request-driven services: Rate, Errors, Duration, per service and per route.
- **USE** for resources: Utilization, Saturation, Errors, for CPU, memory, disks, connection pools, queues.
- **The four golden signals** (Google SRE): latency, traffic, errors, saturation.

Structure dashboards top down: a service overview with SLO status and RED at the top, then dependencies (Postgres, Redis, external APIs), then resource panels. Overlay deploy annotations so "did the deploy cause this?" is visible at a glance. Show percentiles and error ratios, not averages. Keep dashboards as code (Grafana provisioning from JSON in Git, or generated with Grafonnet or the Grafana Terraform provider), and delete dashboards nobody opens. Deeper latency and profiling views are in [performance monitoring]({{ '/backend/performance-monitoring/' | relative_url }}).

## In practice

A runnable stdlib JSON formatter with a correlation ID and tenant bound per request through `contextvars`, redaction of sensitive keys and emails, plus the bucket interpolation `histogram_quantile` performs:

```python
import contextvars
import io
import json
import logging
import re
import uuid

request_id = contextvars.ContextVar("request_id", default="-")
tenant_id = contextvars.ContextVar("tenant_id", default="-")

SENSITIVE = {"password", "token", "authorization", "card_number", "secret"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub(value):
    return EMAIL.sub("[email]", value) if isinstance(value, str) else value


class JsonFormatter(logging.Formatter):
    def format(self, record):
        doc = dict(
            ts=self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            level=record.levelname,
            logger=record.name,
            event=scrub(record.getMessage()),
            request_id=request_id.get(),
            tenant_id=tenant_id.get(),
            service="orders-api",
        )
        for key, value in getattr(record, "fields", {}).items():
            doc[key] = "[redacted]" if key.lower() in SENSITIVE else scrub(value)
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc)


def handle_request(log, headers, tenant):
    # middleware: accept an upstream ID or mint one, then bind it for this request
    rid = headers.get("X-Request-ID") or uuid.uuid4().hex
    t1, t2 = request_id.set(rid), tenant_id.set(tenant)
    try:
        fields = dict(user_email="ana@example.com", password="hunter2", amount=4200)
        log.info("payment captured", extra=dict(fields=fields))
        return rid
    finally:
        request_id.reset(t1)
        tenant_id.reset(t2)


def histogram_quantile(q, buckets):
    """Prometheus-style estimate from cumulative (upper_bound, count) buckets."""
    total = buckets[-1][1]
    rank = q * total
    prev_bound, prev_count = 0.0, 0
    for bound, count in buckets:
        if count >= rank:
            if bound == float("inf"):
                return prev_bound            # can't interpolate into +Inf
            if count == prev_count:
                return bound
            return prev_bound + (bound - prev_bound) * (rank - prev_count) / (count - prev_count)
        prev_bound, prev_count = bound, count
    return prev_bound


if __name__ == "__main__":
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("orders")
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    rid = handle_request(log, {"X-Request-ID": "req-123"}, tenant="acme")
    line = json.loads(stream.getvalue().splitlines()[0])
    assert rid == "req-123" and line["request_id"] == "req-123"
    assert line["tenant_id"] == "acme" and line["amount"] == 4200
    assert line["password"] == "[redacted]" and line["user_email"] == "[email]"
    assert request_id.get() == "-"           # context does not leak to the next request

    # latency histogram, cumulative counts per upper bound in seconds
    buckets = [(0.1, 600), (0.25, 900), (0.5, 970), (1.0, 995), (float("inf"), 1000)]
    assert abs(histogram_quantile(0.50, buckets) - 0.0833) < 1e-3
    assert abs(histogram_quantile(0.99, buckets) - 0.9) < 1e-9
    assert histogram_quantile(0.999, buckets) == 1.0     # lands in +Inf bucket
    print("logs and histogram ok")
```

Note what the histogram test shows: the true p99 could be anywhere between 0.5 s and 1 s, and the estimate of 0.9 s comes from assuming observations are spread evenly within that bucket.

Illustrative Django metrics middleware with `prometheus_client`, labeling by route template rather than raw path:

```python
import time
from prometheus_client import Counter, Histogram

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["method", "route"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0),
)


class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        match = request.resolver_match
        route = match.route if match else "unmatched"   # "orders/<int:pk>", bounded
        LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        return response
```

An illustrative Fluent Bit agent config: tail container logs, merge the JSON payload into the record, attach Kubernetes metadata, drop health checks, ship to Loki with a few low-cardinality labels.

```text
[INPUT]
    Name              tail
    Path              /var/log/containers/*.log
    multiline.parser  docker, cri
    Tag               kube.*

[FILTER]
    Name              kubernetes
    Match             kube.*
    Merge_Log         On
    Keep_Log          Off

[FILTER]
    Name              grep
    Match             kube.*
    Exclude           path ^/(healthz|readyz)$

[OUTPUT]
    Name              loki
    Match             kube.*
    Host              loki.observability.svc
    Port              3100
    Labels            job=fluent-bit
    Label_keys        $kubernetes['namespace_name'],$kubernetes['labels']['app']
```

## Pitfalls

- **Unstructured, interpolated messages.** Symptom: queries are regexes, dashboards built from log parsing break on wording changes. Fix: JSON with constant event names and separate fields.
- **Missing correlation across async work.** Symptom: the API log has a request ID, the Celery task that failed doesn't. Fix: propagate the ID (or trace context) in task headers and bind it in a `task_prerun` hook.
- **Secrets in logs.** Symptom: a token appears in Kibana; now it must be rotated and the index purged. Fix: redaction at source, deny-list keys, pipeline backstop, never log raw headers or bodies.
- **Cardinality explosion.** Symptom: Prometheus memory doubles after a deploy, then OOMs. Fix: route templates, no IDs in labels, relabel drops, alerts on series growth.
- **Averaging percentiles.** `avg(p99)` across pods is not the fleet p99. Fix: histograms, sum buckets, then `histogram_quantile`.
- **`sum` before `rate`.** Counter resets in one pod produce negative or huge spikes. Fix: `sum(rate(...))`.
- **Debug logging left on.** Symptom: log bill triples, the pipeline drops data under backpressure. Fix: dynamic log levels, sampling, budgets per service.
- **Per-worker metrics in Gunicorn.** Symptom: counters jump up and down between scrapes. Fix: multiprocess mode.

## Interview questions

**Logs vs metrics vs traces?** Metrics are cheap aggregates for alerting and trends; logs are detailed per-event records for investigation; traces show one request's path and timing across services. Link them with shared IDs and exemplars.

**How do you propagate a correlation ID?** Accept or generate it at the edge, store it in a context variable, add it to every log line in the formatter, forward it on outbound HTTP calls and in message headers for async work, and return it in the response. Prefer the W3C trace ID when tracing exists.

**Why does Prometheus pull?** Scrape failure is itself a health signal (`up`), configuration and rate are controlled centrally, and targets stay simple. Push fits short-lived jobs and cross-network sources, via Pushgateway or OTLP.

**What is cardinality and why does it matter?** The number of unique label combinations; each is a series stored in memory. Unbounded labels like user IDs or raw paths multiply series into the millions and take down the metrics system. Keep labels bounded and put IDs in logs and traces.

**Histogram or summary for latency?** Histogram: buckets aggregate across instances, so you can compute fleet-wide percentiles and SLO ratios. Summaries compute quantiles per instance that can't be combined. Pick bucket boundaries around your SLO thresholds.

**Loki or Elasticsearch?** Loki indexes only labels and stores compressed chunks in object storage, so it is cheap and good for "this service, this time, grep". Elasticsearch indexes everything for fast arbitrary search but costs far more to run at volume.

**How do you keep PII out of logs?** Log identifiers, not personal data; redact sensitive keys and patterns in the formatter; add a pipeline redaction backstop; restrict access; set retention limits; use keyed hashes when correlation on personal data is required.

## Further reading

- *Site Reliability Engineering* (Google, 2016), chapter "Monitoring Distributed Systems".
- Prometheus documentation: Metric types, Histograms and summaries, Querying basics, and Instrumentation best practices.
- *Observability Engineering*, Charity Majors, Liz Fong-Jones and George Miranda (O'Reilly, 2022).
- Grafana Loki documentation: Labels and cardinality best practices.
- *Systems Performance*, Brendan Gregg (Addison-Wesley, 2nd edition), for the USE method.
