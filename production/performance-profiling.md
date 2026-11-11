---
title: Performance profiling
section: production
order: 9
summary: Find the real bottleneck with cProfile, py-spy, django-silk and pg_stat_statements, then confirm the fix under load with Locust or k6.
---

Most performance work fails because people optimize what they assume is slow. In a typical Django service the time goes to the database (too many queries, or one bad plan), to waiting on something external, or to serialization of large payloads. Pure Python CPU is rarely the first problem. Profiling replaces the guess with a measurement, and load testing tells you whether the fix holds at real concurrency.

## How it works

### Measure, then split

Before reaching for a profiler, decompose one slow request:

```text
wall time  = app CPU + DB time + external I/O + waiting (pool, locks, GIL, queue)
```

Your request log or trace should already give `duration_ms`, `db_ms`, `db_queries` and spans for outbound calls. That tells you which tool to pick:

| Symptom | Likely cause | Tool |
|---|---|---|
| Hundreds of queries per request | N+1 | django-silk, debug toolbar, `assertNumQueries` |
| Few queries, high `db_ms` | Bad plan, missing index, huge scan | `EXPLAIN (ANALYZE, BUFFERS)`, `pg_stat_statements` |
| Low `db_ms`, high CPU | Serialization, Python loops, regex, JSON | cProfile, py-spy |
| Low CPU and low DB, still slow | Waiting: pool, locks, upstream, GIL | Traces, py-spy dump, `pg_stat_activity` |
| Fine alone, slow under load | Contention, pool size, worker count | Load test plus USE metrics |

### Deterministic vs sampling profilers

- **Deterministic** (cProfile): hooks every function call and return. Exact call counts, but adds overhead (often 2x or more) and distorts code with many tiny calls. Good locally and in tests.
- **Sampling** (py-spy): reads the stack of a running process from outside, ~100 times per second. Near-zero overhead, no code changes, safe to attach to production. Gives proportions, not exact counts.

### Reading a profile

- **Cumulative time** (`cumtime`): time in a function including everything it calls. Sort by this to find which high-level operation is expensive.
- **Total/self time** (`tottime`): time in the function's own code. Sort by this to find the hot leaf.
- **Flame graphs**: x-axis is proportion of samples (not time order), y-axis is stack depth. Look for wide plateaus.

## In practice

### cProfile on a single code path

```python
import cProfile, pstats

with cProfile.Profile() as pr:
    build_monthly_report(tenant_id=512, month="2026-02")

stats = pstats.Stats(pr).sort_stats("cumulative")
stats.print_stats(25)
```

Or from the shell for a management command:

```bash
python -m cProfile -o out.prof manage.py rebuild_rollups --tenant 512
python -m pstats out.prof   # then: sort cumtime, stats 30
snakeviz out.prof           # browser view
```

### py-spy on a live process

```bash
# what is every thread doing right now? (stuck worker, deadlock)
py-spy dump --pid 12345

# live top-like view of hot functions
py-spy top --pid 12345

# record a flame graph for 60 seconds
py-spy record --pid 12345 --duration 60 --output profile.svg
```

`py-spy dump` is the first thing to run on a hung gunicorn worker or a task that has been "running" for an hour. In containers it needs the `SYS_PTRACE` capability.

### django-silk for request-level profiling

Silk records every request with its SQL queries, query count, time per query and where in your code each query came from. Enable in development or staging, not on all production traffic.

```python
INSTALLED_APPS += ["silk"]
MIDDLEWARE = ["silk.middleware.SilkyMiddleware", *MIDDLEWARE]
SILKY_PYTHON_PROFILER = True        # cProfile per request
SILKY_INTERCEPT_PERCENT = 10        # sample if left on in staging
```

Typical find: a serializer touching `invoice.customer.account_manager` per row, 3 queries x 200 rows = 600 queries. Fix:

```python
qs = (Invoice.objects
      .filter(tenant=tenant)
      .select_related("customer__account_manager")
      .prefetch_related("line_items"))
```

Lock it in with a test so it doesn't regress:

```python
def test_invoice_list_query_count(client, django_assert_max_num_queries):
    with django_assert_max_num_queries(8):
        client.get("/api/invoices/?page_size=200")
```

### pg_stat_statements for the database side

```sql
SELECT left(query, 90) AS query, calls,
       round(mean_exec_time::numeric, 1) AS mean_ms,
       round(total_exec_time::numeric / 1000, 0) AS total_s,
       shared_blks_read, shared_blks_hit
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

Then `EXPLAIN (ANALYZE, BUFFERS)` the top offenders with realistic parameters (the largest tenant, not the smallest). Common fixes, in order of effort:

1. Missing or wrong index: composite index matching `WHERE tenant_id = ? AND status = ? ORDER BY created_at`; partial index for a hot subset (`WHERE status = 'pending'`).
2. Fetching too much: `.only()` / `.values()`, pagination with keyset instead of `OFFSET`.
3. Repeated aggregation over raw rows: precompute rollups (daily per-tenant summaries) and query those. Dashboards that scan 50M event rows on every load are a classic case where a rollup table turns minutes into sub-second.
4. Plan instability from stale stats: `ANALYZE`, raise `default_statistics_target` on skewed columns.

### Load testing

A profile of one request doesn't show contention. Load testing does: connection pool exhaustion, lock contention, worker saturation, cache stampedes.

Locust (Python):

```python
from locust import HttpUser, task, between

class TenantUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.client.headers["Authorization"] = "Bearer test-token"

    @task(5)
    def list_invoices(self):
        self.client.get("/api/invoices/?page_size=50", name="/api/invoices/")

    @task(1)
    def dashboard(self):
        self.client.get("/api/dashboard/summary/", name="/api/dashboard/")
```

```bash
locust -f loadtest.py --host https://staging.example.com \
  --users 200 --spawn-rate 20 --run-time 10m --headless
```

k6 (JavaScript) with pass/fail thresholds, useful in CI:

```javascript
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  stages: [
    { duration: "2m", target: 100 },
    { duration: "5m", target: 100 },
    { duration: "1m", target: 0 },
  ],
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<800"],
  },
};

export default function () {
  const res = http.get(`${__ENV.BASE_URL}/api/invoices/?page_size=50`);
  check(res, { "status 200": (r) => r.status === 200 });
  sleep(1);
}
```

Load testing rules:

- Use production-like data volumes and a realistic mix of endpoints, including the big tenants.
- Ramp up and hold; watch where latency bends, not just the final number.
- Watch the server side (CPU, DB connections, pool wait, queue depth) while the test runs. The client numbers only tell you *that* it broke.
- Change one thing between runs.

## Pitfalls

- **Optimizing without a baseline.** Record p50/p95/p99 and query counts before changing anything.
- **Profiling with tiny data.** The local DB with 200 rows will never show the seq scan that hurts at 20M rows.
- **Micro-optimizing Python** while 400 queries run per request.
- **Profiler overhead** misleading you: cProfile inflates code with many small function calls. Cross-check with py-spy.
- **Caching as the first fix.** It hides the slow query, adds invalidation bugs, and the cold path still times out. Fix the query, then cache if needed.
- **Load testing production** without warning anyone, or against third-party APIs that bill per call.

## Rules of thumb

- Split wall time first; pick the tool by where the time goes.
- py-spy for live processes, cProfile for a reproducible code path, silk for per-request SQL.
- `pg_stat_statements` sorted by total time finds what matters most to the system.
- Test with the largest tenant's data.
- Guard every fix with a query-count or latency test.
- Confirm under load before calling it done.

## Interview angle

- **"An endpoint is slow. How do you find out why?"** Check the request's time split from traces/logs; if many queries, fix N+1 with `select_related`/`prefetch_related`; if a few slow ones, `EXPLAIN ANALYZE` and index or restructure; if CPU, profile with py-spy/cProfile; confirm with a load test.
- **"Sampling vs deterministic profiler?"** Sampling is low overhead and production-safe but statistical; deterministic is exact but slow and can distort results.

Related: [Debugging production issues]({{ '/production/debugging-production/' | relative_url }}), [Observability]({{ '/production/observability/' | relative_url }}).
