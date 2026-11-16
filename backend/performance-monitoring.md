---
title: Performance monitoring
section: backend
order: 62
phase: "Phase 8 · Performance and reliability"
summary: Measure latency with percentiles and histograms, apply RED and USE, define SLIs and SLOs with burn-rate alerts, and use APM and continuous profiling.
---

Performance monitoring answers two questions continuously: are users getting a fast, correct experience right now, and if not, where is the time going? The first needs a small set of user-facing indicators (latency percentiles, error rates, SLOs) measured correctly. The second needs tools that break a slow request into its parts: traces, resource metrics and profiles. Most monitoring failures come from measuring the wrong thing, usually an average, rather than from missing tools.

```text
          symptoms: alert on these         causes: investigate with these
 +----------------------------------+   +--------------------------------+
 | SLIs and SLOs (good / valid)     |   | traces: per-request breakdown  |
 | RED per endpoint: rate, errors,  |-->| USE per resource: CPU, memory, |
 |   duration percentiles           |   |   pools, queues, disks         |
 | Apdex / user-facing summaries    |   | profiles: per-function CPU     |
 +----------------------------------+   +--------------------------------+
```

## Performance monitoring

### Latency percentiles and why averages lie

Latency distributions are skewed and often multimodal: most requests hit the cache and take 40 ms, a few miss and take 2 s. The mean of that distribution, about 90 ms, describes **no actual request**. It hides the slow mode entirely and moves when traffic mix changes even if no request got slower.

Percentiles describe the distribution directly. p50 is the typical request, p99 is the slowest 1%. Watch p50, p95, p99 and max (or p99.9), per endpoint.

Why the tail matters more than it looks:

- **Users make many requests.** A page that triggers 20 API calls has a much higher chance than 1% of including at least one p99 request.
- **Fan-out amplifies it.** If a request calls 100 backends in parallel and waits for all, the chance at least one is slower than its p99 is `1 − 0.99^100 ≈ 63%`. Dean and Barroso's "The Tail at Scale" is the classic treatment.
- **Your best customers hit it most.** The largest tenants have the most data and the slowest queries.

Two rules for computing percentiles:

1. **Never average percentiles.** The mean of each pod's p99 is not the fleet's p99, and the mean of hourly p99s is not the daily p99. Percentiles can't be combined from summaries.
2. **Aggregate distributions, then take the percentile.** Store histograms (bucket counts add across pods and time) or mergeable sketches (HdrHistogram, t-digest, DDSketch), then compute the quantile from the merged result. Prometheus `summary` metrics compute quantiles in each process and can't be aggregated; prefer `histogram`, and place bucket boundaries at your SLO thresholds.

Also measure where it matters. Server-side latency excludes load balancer queueing, TLS, client networks and browser time; complement it with load balancer metrics and real-user or synthetic monitoring. Record latency for failed and timed-out requests too, or errors disappear from the latency picture.

### RED and USE methods

| Method | Apply to | Signals | Origin |
|---|---|---|---|
| RED | Every request-driven service and endpoint | **R**ate, **E**rrors, **D**uration (as a distribution) | Tom Wilkie |
| USE | Every resource | **U**tilization, **S**aturation, **E**rrors | Brendan Gregg |
| Four golden signals | User-facing systems | Latency, traffic, errors, saturation | Google SRE book |

RED tells you a service is unhealthy; USE tells you which resource is why. For a Django service, the USE checklist is CPU (and throttling), memory, gunicorn worker and thread saturation, DB connection pool usage and wait time, Celery queue depth and age, Postgres connections, locks and replication lag, and Redis memory and evictions. Saturation is the most predictive: a pool at 100% with waiters is already adding latency.

### Apdex

Apdex reduces latency to a 0–1 user-satisfaction score with a target threshold `T`:

- **Satisfied:** response time ≤ T.
- **Tolerating:** between T and 4T.
- **Frustrated:** over 4T, or an error.
- `Apdex = (satisfied + tolerating / 2) / total`.

It's easy to explain to non-engineers and comparable across services. But it hides how bad the frustrated requests are: 3% of requests at 2.5 s and 3% at 25 s score the same. The demo below shows an excellent Apdex alongside a two-second p99. Use it as a summary, not as your SLO.

### APM tools

Application performance monitoring agents (Datadog APM, New Relic, Dynatrace, Elastic APM, Sentry Performance, Honeycomb, Grafana's stack) auto-instrument frameworks and clients (Django, psycopg, `requests`/`httpx`, Redis, Celery) and give you:

- Per-endpoint RED dashboards and slowest-trace drill-down.
- A breakdown of each request into DB queries, cache calls and outbound HTTP calls, which is where N+1 patterns and slow queries surface.
- Service maps, error tracking with stack traces, and deployment markers to correlate regressions with releases.

Instrumenting with OpenTelemetry keeps you vendor-neutral: the same spans and metrics can go to any backend. See [Distributed tracing and OpenTelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}) and [Logging and metrics]({{ '/backend/logging-and-metrics/' | relative_url }}). With trace sampling, use tail-based sampling or always keep slow and error traces. Otherwise the rare slow request you care about is the one dropped.

### SLIs and SLOs

- **SLI:** a ratio of good events to valid events, measured as close to the user as practical. Availability SLI: non-5xx responses / all responses. Latency SLI: requests faster than 300 ms / all requests.
- **SLO:** a target for an SLI over a window, for example "99.9% of checkout requests succeed and 99% complete under 300 ms, over 30 days".
- **Error budget:** `1 − SLO`. 99.9% over 30 days allows 43.2 minutes of full outage, or 0.1% of requests failing. Budget remaining decides whether to ship features or invest in reliability.
- **Burn-rate alerts.** Alert on how fast the budget is being consumed rather than on raw error rate. The SRE Workbook's multi-window scheme pages when the burn rate is ≥ 14.4 over both 1 hour and 5 minutes (2% of a 30-day budget gone in an hour). It opens a ticket for slower burns over 6 hours or 3 days. The long window gives significance, the short window makes the alert reset quickly after recovery.

Pick few SLOs, on user journeys (login, checkout, search), not on every endpoint. More in [Alerting and incidents]({{ '/backend/alerting-and-incidents/' | relative_url }}).

### Continuous profiling

Continuous profiling runs a low-overhead sampling profiler on production processes all the time and stores the stacks. You can ask "which functions used the most CPU in the API fleet yesterday between 14:00 and 14:15?" or diff flame graphs between two releases. Google described the approach in "Google-Wide Profiling" (2010). Grafana Pyroscope, Parca, Datadog's Continuous Profiler and Google Cloud Profiler provide it now, some using eBPF to profile without touching the process. Overhead is designed to be low, typically a few percent at most.

It answers what metrics and traces can't: why a service needs 30% more CPU after a release, which code path allocates the most memory, and what a slow span was actually doing. Some tools link profiles to trace spans. The profiling techniques themselves are in [Memory management and CPU profiling]({{ '/backend/memory-and-cpu-profiling/' | relative_url }}).

## In practice

### Instrumenting Django with a Prometheus histogram (illustrative)

```python
import time
from prometheus_client import Histogram

REQUEST_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5),  # 0.3 = the SLO threshold
)


class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        match = request.resolver_match
        route = match.route if match else "unmatched"   # template, not raw path
        REQUEST_SECONDS.labels(request.method, route, response.status_code).observe(
            time.perf_counter() - start
        )
        return response
```

Under gunicorn each worker is a separate process with its own registry; `prometheus_client` needs its multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`) or you scrape one random worker. Packages like `django-prometheus` handle this wiring.

### RED and SLI queries (PromQL)

```text
# Rate: requests per second by route
sum by (route) (rate(http_request_duration_seconds_count[5m]))

# Errors: share of 5xx responses
sum(rate(http_request_duration_seconds_count{status=~"5.."}[5m]))
  / sum(rate(http_request_duration_seconds_count[5m]))

# Duration: fleet-wide p99 (merge buckets across pods, then quantile)
histogram_quantile(0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# Latency SLI: share of requests under 300 ms (needs a bucket at 0.3)
sum(rate(http_request_duration_seconds_bucket{le="0.3"}[5m]))
  / sum(rate(http_request_duration_seconds_count[5m]))
```

### Percentiles, histograms, Apdex and budgets, runnable

```python
import math
import random


def percentile(samples, p):
    """Nearest-rank percentile."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def bimodal(n, seed, fast=(20, 60), slow=(800, 2500), slow_share=0.03):
    rng = random.Random(seed)
    return [rng.uniform(*(slow if rng.random() < slow_share else fast)) for _ in range(n)]


def histogram_quantile(q, bounds, samples):
    """Prometheus-style estimate: cumulative buckets plus linear interpolation."""
    cumulative = [sum(1 for s in samples if s <= b) for b in bounds]
    rank = q * len(samples)
    lower, below = 0.0, 0
    for upper, count in zip(bounds, cumulative):
        if count >= rank:
            return lower + (upper - lower) * (rank - below) / (count - below)
        lower, below = upper, count
    return bounds[-1]


def apdex(samples, t):
    satisfied = sum(1 for s in samples if s <= t)
    tolerating = sum(1 for s in samples if t < s <= 4 * t)
    return (satisfied + tolerating / 2) / len(samples)


def p_any_slow(fanout, p=0.99):
    """Chance that at least one of `fanout` parallel calls exceeds its p-quantile."""
    return 1 - p ** fanout


def budget_minutes(slo, days=30):
    return (1 - slo) * days * 24 * 60


if __name__ == "__main__":
    ms = bimodal(10_000, seed=7)
    mean = sum(ms) / len(ms)
    p50, p99 = percentile(ms, 50), percentile(ms, 99)
    print(f"mean {mean:.0f} ms  p50 {p50:.0f} ms  p99 {p99:.0f} ms  max {max(ms):.0f} ms")
    assert p50 < mean < p99
    assert not any(60 < x < 800 for x in ms)         # the mean describes no request

    # Averaging per-host p99s is wrong: merge the data (or histograms) instead.
    healthy = bimodal(9_000, seed=1, slow_share=0.0)
    degraded = [random.Random(2).uniform(200, 2000) for _ in range(1_000)]
    averaged = (percentile(healthy, 99) + percentile(degraded, 99)) / 2
    true_p99 = percentile(healthy + degraded, 99)
    print(f"average of host p99s {averaged:.0f} ms vs true fleet p99 {true_p99:.0f} ms")
    assert true_p99 > 1.3 * averaged

    bounds = [25, 50, 100, 200, 300, 500, 1000, 2500, 5000]
    estimate = histogram_quantile(0.99, bounds, ms)
    print(f"histogram p99 estimate {estimate:.0f} ms (exact {p99:.0f} ms)")
    assert abs(estimate - p99) / p99 < 0.1

    score = apdex(ms, t=100)
    print(f"Apdex(T=100 ms) = {score:.2f} despite a {p99 / 1000:.1f} s p99")
    assert score >= 0.94                              # 'excellent' band

    assert round(p_any_slow(100), 2) == 0.63
    assert round(budget_minutes(0.999), 1) == 43.2
    assert math.isclose(14.4 * 1 / (30 * 24), 0.02)   # 1 h at 14.4x burns 2%
```

## Pitfalls

- **Averages on the main dashboard.** They hide the tail and drift with traffic mix. Lead with p50, p95 and p99 from histograms.
- **Averaging percentiles.** Across pods, regions or time windows. Merge histograms first.
- **Bucket boundaries that miss the SLO.** If buckets jump from 250 ms to 500 ms, you can't measure a 300 ms SLI precisely. Put a boundary at each threshold.
- **Label cardinality explosions.** Raw paths with ids, user ids or tenant ids as metric labels create millions of time series and take down Prometheus. Use route templates; put per-tenant detail in traces and logs, or track only the top tenants.
- **Percentiles on tiny samples.** p99 over 50 requests is effectively the max. Use longer windows for low-traffic endpoints, or alert on SLO burn instead.
- **Only server-side numbers.** Queueing at the load balancer, slow clients and DNS/TLS don't appear in app metrics. Add LB metrics and real-user or synthetic monitoring.
- **Coordinated omission in your own clients.** A synchronous monitor that waits on a hung request records one data point per hang; see [Load and stress testing]({{ '/backend/load-and-stress-testing/' | relative_url }}).
- **Alerting on causes.** Paging on CPU at 80% wakes people for nothing and misses user-facing failures with low CPU. Page on SLO burn; use resource metrics for diagnosis and capacity.

## Interview questions

**Why are averages misleading for latency?**
Latency distributions are skewed and multimodal, so the mean often matches no real request and hides the slow tail. Percentiles describe what users actually experience, especially p99, which large users and fan-out requests hit frequently.

**How do you compute p99 latency across 50 pods?**
Export histograms with fixed buckets from every pod. Sum bucket rates across pods, then compute the quantile from the merged buckets (`histogram_quantile` over `sum by (le)`). Never average the pods' individual p99s, and avoid summary metrics, which can't be merged.

**RED vs USE?**
RED (rate, errors, duration) describes request-serving services from the caller's view. USE (utilization, saturation, errors) describes resources like CPU, pools and disks. RED shows that something is wrong; USE helps find which resource is the bottleneck.

**What are SLIs, SLOs and error budgets, and how do you alert on them?**
An SLI is a good/valid ratio such as requests under 300 ms. An SLO is a target for it over a window, like 99.9% over 30 days. The error budget is the allowed failure, `1 − SLO`. Alert on burn rate with multiple windows, paging when you'd exhaust the budget in days and ticketing for slow burns.

**What is Apdex and what's wrong with it?**
A 0–1 score from counting satisfied (≤ T), tolerating (≤ 4T) and frustrated requests. It's simple to communicate but discards how slow the slow requests are, so a terrible tail can still score well.

**Why does tail latency matter more in microservices?**
A user request fans out to many services, and its latency is set by the slowest dependency. With 100 parallel calls, 63% of requests include at least one call slower than its p99. Tail-tolerant techniques like hedged requests, timeouts and load balancing on latency become necessary.

**What does continuous profiling give you that metrics and traces don't?**
Function-level CPU and memory attribution in production over time. You can see which code got more expensive after a release, or what a slow span was actually computing, at low overhead.

**Server p99 looks fine but users report slowness. Where do you look?**
At what server metrics exclude: load balancer queueing and latency, client networks, DNS and TLS, browser rendering, and retries. Also requests that time out and never get recorded. Use real-user monitoring, LB metrics and synthetic checks, and check for coordinated omission in how latency is recorded.

## Further reading

- Betsy Beyer et al., *Site Reliability Engineering*: "Monitoring Distributed Systems" and "Service Level Objectives"; *The Site Reliability Workbook*: "Alerting on SLOs".
- Jeffrey Dean and Luiz André Barroso, "The Tail at Scale" (Communications of the ACM, 2013).
- Brendan Gregg, "The USE Method", and Tom Wilkie's writing on the RED method.
- Prometheus documentation: "Histograms and summaries".
- Gang Ren et al., "Google-Wide Profiling: A Continuous Profiling Infrastructure for Data Centers" (IEEE Micro, 2010).
