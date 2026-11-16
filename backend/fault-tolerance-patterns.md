---
title: Fault tolerance, circuit breakers and bulkheads
section: backend
order: 39
phase: "Phase 5 · Distributed systems"
summary: Keep serving when parts fail with redundancy, failover, timeouts, graceful degradation, circuit breakers, bulkheads, cells and shuffle sharding.
---

The dangerous failure in a backend is rarely a clean crash. It is a dependency that gets slow. Every request waiting on it holds a worker thread, a database connection and some memory, and within seconds the caller runs out of all three and fails requests that never touched the slow dependency. That is a cascading failure. Fault tolerance patterns limit how far a failure spreads: redundancy and failover so one dead node does not matter, timeouts so a slow call cannot hold resources forever, circuit breakers to stop calling something that is down, bulkheads so one dependency cannot take every worker, and cells and shuffle sharding so one bad tenant or deploy reaches only a slice of customers. Retries have their own note: [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}).

## Fault tolerance

Fault tolerance means component faults don't become system failures. Name the faults you design for:

| Fault | Example | Why it hurts |
|---|---|---|
| Crash-stop | Pod OOM-killed, node lost | Easy: health checks remove it |
| Omission | Dropped packets, lost message | Looks like slowness until a timeout fires |
| Timing (slow) | GC pause, lock contention, full disk queue | Holds caller resources; the usual cascade trigger |
| Gray failure | One AZ drops 2% of packets | Health checks pass while users see errors |

### Redundancy

Run more than one of everything that matters, spread across failure domains: hosts, racks, availability zones, sometimes regions. Stateless app servers are cheap to duplicate behind a load balancer; stateful components need [replication]({{ '/backend/replication/' | relative_url }}).

Capacity is part of redundancy. N+1 means you can lose one unit and still carry peak load. The AWS Builders' Library calls the stronger form *static stability*: losing an AZ requires no reaction at all, not even autoscaling, because the control plane you would scale with may be impaired by the same incident. With three AZs, each runs at no more than about two-thirds utilization so the other two can absorb its share.

| | Active-passive | Active-active |
|---|---|---|
| Standby serves traffic | No | Yes |
| Failover time | Seconds to minutes | Near zero (remove the bad node) |
| Cost | Idle capacity | Capacity used, but needs headroom |
| Hard part | Promotion, split brain | Conflicting writes, data placement |
| Typical use | Primary database, singleton schedulers | Stateless services, caches, read replicas |

### Failover

Failover has three steps: detect (missed heartbeats, failed health checks), decide (a consensus system or single orchestrator picks the new primary), and switch (move a virtual IP, update DNS, or let clients find the new primary). The detection timeout is the key trade-off: too short and a GC pause triggers a needless failover, possibly flapping; too long and requests fail until it fires. The old primary must be fenced so it cannot keep accepting writes, or you get split brain (see [Leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }})).

Clients take part too. libpq accepts several hosts and picks the writable one: `postgresql://db1,db2/app?target_session_attrs=read-write`. DNS failover only works if clients honor short TTLs and reconnect. Measure failover against RTO (how long you can be down) and RPO (how much data you can lose), and rehearse it; a failover path that has never run usually fails the first time.

### Timeouts

Every network call needs a timeout, and many libraries default to none: Python `requests` waits forever unless you pass `timeout`. Use a short connect timeout (a few hundred milliseconds inside a data center) and a read timeout from the dependency's measured latency, commonly its p99.9 plus margin.

Timeouts must nest: if the edge gives a request 3 s, an inner call with a 5 s timeout works on something nobody is waiting for. Pass a deadline down the chain instead (see [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})). In PostgreSQL, `statement_timeout`, `lock_timeout` and `idle_in_transaction_session_timeout` stop one bad query or forgotten transaction from holding locks and connections.

### Graceful degradation

Decide in advance what each feature does when a dependency fails. Sort dependencies into critical (checkout needs payments) and non-critical (a product page renders fine without recommendations). For non-critical ones, fall back to stale cache, a default, a hidden widget or a read-only mode, and queue writes to apply later.

Load shedding applies the same idea to your own service: when over capacity, reject early with 429 or 503, low-priority traffic first, instead of letting latency rise for everyone. Kill switches, flags that turn off an expensive feature in seconds, are the manual version.

## Circuit breaker pattern

A circuit breaker wraps calls to one dependency and tracks their outcomes. When too many fail, it opens and rejects calls immediately for a cool-down period without touching the network, freeing the caller's threads and giving the dependency room to recover. Michael Nygard popularized it in *Release It!*.

```text
                 failure rate >= threshold
      +--------+ (once min_calls seen)     +--------+
      | CLOSED | ------------------------> |  OPEN  |  reject at once,
      +--------+                           +--------+  serve fallback
          ^                                  |    ^
          | N probes                cool-down|    | a probe
          | succeed                  elapsed |    | fails
          |                                  v    |
          |                            +-----------+
          +----------------------------| HALF-OPEN |
                                       +-----------+
                                       allow N probes
```

- **Closed**: calls pass through; outcomes go into a sliding window.
- **Open**: calls fail fast with an error the caller turns into a fallback.
- **Half-open**: after the cool-down, a few probe calls go through. All succeed and the breaker closes; any failure reopens it.

### Thresholds

| Setting | Reasonable start | What it controls |
|---|---|---|
| Failure rate threshold | 50% | Share of failed calls in the window that trips it |
| Sliding window | Last 20–100 calls or 10–60 s | How much history decides |
| Minimum calls | 10–20 | No tripping on 1 failure out of 2 calls |
| Slow-call threshold | Near the dependency's p99 | Treat slow successes as failures |
| Open duration | 5–60 s | How long to fail fast before probing |
| Half-open probes | 1–10 | Trial calls allowed during recovery |

Count only failures that reflect the dependency's health: connection errors, timeouts, 5xx and slow calls (a dependency that answers in 20 s is effectively down). A 400 or 404 means the caller sent a bad request; counting it lets one buggy client open the breaker for everyone.

Scope breakers per dependency, sometimes per endpoint. State is normally local to each process, which works at real traffic levels and avoids a shared store that becomes its own dependency. Resilience4j (Java), Polly (.NET) and pybreaker (Python) implement the pattern; Netflix's Hystrix has been in maintenance mode since 2018. In Envoy and Istio, *outlier detection* is the per-host breaker (eject a host after consecutive 5xx), while Envoy's settings named "circuit breaking" are concurrency caps, which makes them bulkheads.

A runnable breaker with a count-based failure-rate window, a minimum call count, slow-call detection and limited half-open probes:

```python
import threading
import time
from collections import deque


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, failure_rate=0.5, window=20, min_calls=10, slow_call_seconds=2.0,
                 open_seconds=30.0, half_open_probes=3,
                 failure_on=(ConnectionError, TimeoutError), clock=time.monotonic):
        self.failure_rate, self.min_calls = failure_rate, min_calls
        self.slow_call_seconds, self.open_seconds = slow_call_seconds, open_seconds
        self.half_open_probes, self.failure_on, self.clock = half_open_probes, failure_on, clock
        self.results = deque(maxlen=window)  # True means the call failed
        self.state, self.opened_at = "closed", 0.0
        self.probes_left = self.probe_successes = 0
        self.lock = threading.Lock()

    def call(self, fn, *args, **kwargs):
        self._before_call()
        start = self.clock()
        try:
            result = fn(*args, **kwargs)
        except self.failure_on:
            self._record(failed=True)
            raise
        except Exception:
            self._record(failed=False)  # e.g. a validation error: the dependency answered
            raise
        self._record(failed=self.clock() - start > self.slow_call_seconds)
        return result

    def _before_call(self):
        with self.lock:
            if self.state == "open":
                if self.clock() - self.opened_at < self.open_seconds:
                    raise CircuitOpenError("dependency marked down")
                self.state = "half_open"
                self.probes_left, self.probe_successes = self.half_open_probes, 0
            if self.state == "half_open":
                if self.probes_left == 0:
                    raise CircuitOpenError("probes already in flight")
                self.probes_left -= 1

    def _record(self, failed):
        with self.lock:
            if self.state == "half_open":
                if failed:
                    self._trip()
                else:
                    self.probe_successes += 1
                    if self.probe_successes == self.half_open_probes:
                        self.state = "closed"
                        self.results.clear()
            elif self.state == "closed":
                self.results.append(failed)
                if (len(self.results) >= self.min_calls
                        and sum(self.results) / len(self.results) >= self.failure_rate):
                    self._trip()

    def _trip(self):
        self.state, self.opened_at = "open", self.clock()
        self.results.clear()


if __name__ == "__main__":
    now = [0.0]
    cb = CircuitBreaker(window=10, min_calls=4, open_seconds=30, half_open_probes=2,
                        clock=lambda: now[0])

    def ok():
        return "ok"

    def refused():
        raise ConnectionError("connection refused")

    def bad_request():
        raise ValueError("400: missing field")

    def attempt(fn):
        try:
            return cb.call(fn)
        except (ConnectionError, ValueError, CircuitOpenError) as exc:
            return type(exc).__name__

    for fn in (ok, refused, ok):
        attempt(fn)
    assert cb.state == "closed"              # 1 of 3 failed and below min_calls anyway
    attempt(refused)
    assert cb.state == "open"                # 2 of 4 = 50%

    assert attempt(ok) == "CircuitOpenError"   # fails fast, ok() never runs

    now[0] += 31                             # cool-down over: half-open
    attempt(refused)
    assert cb.state == "open"                # a failed probe reopens

    now[0] += 31
    assert attempt(ok) == "ok" and cb.state == "half_open"
    assert attempt(ok) == "ok" and cb.state == "closed"

    for _ in range(10):                      # client errors never trip it
        attempt(bad_request)
    assert cb.state == "closed"

    def slow():
        now[0] += 5                          # succeeds, but after 5 s
        return "late"

    for _ in range(5):
        attempt(slow)
    assert cb.state == "open"                # 5 slow of the last 10 calls = 50%
    print("circuit breaker ok")
```

## Bulkhead pattern

A ship's hull is split into watertight compartments so one breach does not sink it. In a service, the compartments are resource pools. Without them, a payment provider that starts taking 30 s per call slowly absorbs all 200 request threads, and search, login and health checks fail too. A bulkhead caps how many threads, connections or workers any one dependency may hold.

```text
   request threads (shared pool of 200)
    |   |   |   |   |   |   |   |   |
    v   v   v   v   v   v   v   v   v
 +-----------+  +-----------+  +-----------+
 | payments  |  |  search   |  | recommend |   per-dependency
 |  max 20   |  |  max 40   |  |  max 10   |   concurrency caps
 +-----------+  +-----------+  +-----------+
       |              |              |
  Payments API     Search svc     Recs svc
  (30 s stalls)    (healthy)      (healthy)

 20 payment calls stuck, the 21st rejected at once;
 search and recommendations keep every one of their slots.
```

### Pool isolation

| Isolation | How | Pros | Cons |
|---|---|---|---|
| Semaphore | Concurrency cap on the caller's threads | Cheap | A stuck call blocks its caller until timeout |
| Thread pool per dependency | Calls run on a small dedicated pool | Caller can time out and walk away | Thread overhead |
| Connection pool per dependency | Separate HTTP or DB pools, each capped | Natural for HTTP and DB clients | Must size each pool |
| Process or deployment | Separate workers or services per workload | Strongest isolation | More moving parts |

In a Django stack the process level is often the most useful: separate Celery queues and workers for slow reports versus transactional emails, a separate Gunicorn deployment for exports, and a smaller database pool (or a role with its own `CONNECTION LIMIT`) for batch jobs so they cannot starve the API. See [Connection pooling]({{ '/backend/connection-pooling/' | relative_url }}).

### Per-dependency limits

Size each compartment with Little's law: concurrency = throughput × latency. A dependency called 200 times per second at 50 ms p99 needs about 10 slots; give it 15–20. When full, reject immediately or after a very short wait; a large queue in front of a bulkhead just moves the pile-up into memory. Per-tenant limits apply the same idea to customers, so one tenant's bulk import cannot starve the rest (see [Multi-tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }})).

A runnable semaphore bulkhead. The payments compartment fills up while search keeps working:

```python
import threading
from concurrent.futures import ThreadPoolExecutor


class BulkheadFull(Exception):
    pass


class Bulkhead:
    """Cap concurrent calls into one dependency; reject instead of queueing."""

    def __init__(self, name, max_concurrent, max_wait=0.0):
        self.name, self.max_wait = name, max_wait
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self.rejected = 0

    def call(self, fn, *args, **kwargs):
        if not self.slots.acquire(timeout=self.max_wait):
            self.rejected += 1
            raise BulkheadFull(self.name)
        try:
            return fn(*args, **kwargs)
        finally:
            self.slots.release()


if __name__ == "__main__":
    payments = Bulkhead("payments", max_concurrent=2)
    search = Bulkhead("search", max_concurrent=4)
    provider_hangs = threading.Event()
    both_stuck = threading.Barrier(3)        # two payment calls plus the main thread

    def charge():
        both_stuck.wait()
        provider_hangs.wait(5)               # the payment provider stalls
        return "charged"

    with ThreadPoolExecutor(max_workers=8) as request_threads:
        stuck = [request_threads.submit(payments.call, charge) for _ in range(2)]
        both_stuck.wait()                    # both payment slots are now held

        try:
            payments.call(charge)
            raise AssertionError("third payment call should be rejected")
        except BulkheadFull:
            pass
        assert payments.rejected == 1

        hits = [request_threads.submit(search.call, lambda: "hits") for _ in range(4)]
        assert [f.result(timeout=1) for f in hits] == ["hits"] * 4

        provider_hangs.set()
        assert [f.result(timeout=1) for f in stuck] == ["charged", "charged"]

    assert payments.call(lambda: "charged") == "charged"   # slots were released
    print("bulkhead ok")
```

## Fault isolation

Breakers and bulkheads protect one caller from one dependency. Fault isolation asks how many customers a fault reaches when something goes wrong anyway: a poison request, a bad deploy, a runaway tenant.

### Blast radius

Blast radius is the share of users or tenants affected by a single fault. A global config push that breaks parsing has a blast radius of 100%; the same push rolled out one cell at a time has a blast radius of one cell. You shrink it with independent units (cells, AZs, regions), staged rollouts (see [Deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }})), and a data plane that keeps serving on its last known good configuration when the control plane is down.

### Cells

A cell-based architecture runs many complete, independent copies of the stack, each serving a fixed subset of customers. A thin router maps a partition key such as `tenant_id` to a cell and does little else, so it rarely changes and rarely breaks.

```text
                 +-----------------------+
  request ---->  |  cell router           |  thin, static mapping
  (tenant_id)    |  tenant_id -> cell     |  rarely deployed
                 +-----------------------+
                   |          |         |
           +----------+ +----------+ +----------+
           |  cell 1  | |  cell 2  | |  cell 3  |
           |  LB, app | |  LB, app | |  LB, app |
           |  DB,cache| |  DB,cache| |  DB,cache|
           +----------+ +----------+ +----------+
           tenants A-F  tenants G-P  tenants Q-Z
```

A cell has a maximum size, which caps how big any database or queue gets; growth means adding cells. AWS has written publicly about cell-based architecture, and Slack has described moving to AZ-aligned cells. The costs: cross-cell features (global search, admin reports) need a separate aggregation path, moving a tenant is a migration, and you operate many copies of everything.

### Shuffle sharding

Plain sharding splits 8 workers into 4 pairs; a customer whose requests crash workers takes out its pair and a quarter of all customers with it. Shuffle sharding gives each customer its own random combination of workers. With 8 workers and shards of 2 there are 28 combinations, so a poison customer fully takes down only those who share both its workers, about 1 in 28. Everyone else keeps at least one healthy worker and succeeds by retrying on it. The AWS Builders' Library describes Route 53 assigning each hosted zone 4 of 2,048 virtual name servers: roughly 730 billion combinations.

```python
import hashlib
from math import comb


def shuffle_shard(customer, workers, k):
    """Stable choice of k workers per customer (rendezvous hashing)."""
    def score(worker):
        return hashlib.sha256(f"{customer}:{worker}".encode()).digest()
    return frozenset(sorted(workers, key=score)[:k])


if __name__ == "__main__":
    workers = [f"w{i}" for i in range(8)]
    shards = {c: shuffle_shard(c, workers, 2) for c in range(2000)}
    poison = shards[0]          # customer 0's requests crash both of its workers
    fully_down = sum(1 for s in shards.values() if s <= poison) / len(shards)
    assert comb(8, 2) == 28
    assert fully_down < 0.08    # about 1/28, versus 1/4 with fixed pairs
    assert comb(2048, 4) > 7 * 10**11
    print(f"customers fully affected: {fully_down:.3f}")
```

## In practice

Timeouts, statement limits and queue isolation in a Django service (illustrative):

```python
import requests

# (connect, read) in seconds; never call requests without a timeout
resp = requests.get("https://payments.internal/v1/charges/ch_123", timeout=(0.3, 2.0))

# settings.py: cap every query and idle transaction from this app
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "app",
        "OPTIONS": {"options": "-c statement_timeout=5000 -c idle_in_transaction_session_timeout=30000"},
    },
}

# Bulkhead at the process level: slow reports never share workers with emails
CELERY_TASK_ROUTES = {
    "reports.tasks.*": {"queue": "reports"},
    "notifications.tasks.*": {"queue": "notifications"},
}
# celery -A app worker -Q reports --concurrency=4
# celery -A app worker -Q notifications --concurrency=16
```

Bulkheads and per-host breakers in a mesh, with an Istio `DestinationRule` (illustrative; check field names against your Istio version):

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: payments
spec:
  host: payments.prod.svc.cluster.local
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 50          # bulkhead: cap connections to this service
        connectTimeout: 300ms
      http:
        http1MaxPendingRequests: 10 # small queue, then fail fast
        http2MaxRequests: 100
    outlierDetection:               # per-host breaker: eject bad pods
      consecutive5xxErrors: 5
      interval: 10s
      baseEjectionTime: 30s
      maxEjectionPercent: 50
```

## Pitfalls

- **No timeout, or the library default.** Symptom: thread pools pinned at their maximum, latency equal to the load balancer's idle timeout. Fix: explicit connect and read timeouts on every client, plus a total deadline.
- **Inner timeouts longer than outer ones.** Work continues after the caller has given up. Fix: propagate a deadline and derive per-call timeouts from it.
- **Counting 4xx as breaker failures.** One buggy client opens the breaker for every caller.
- **Breaker never trips at low traffic,** or trips on noise. Tune the minimum call count and window to the real per-process call rate.
- **Untested fallback.** It calls another dependency that fails the same way, or has a bug because it never runs. Exercise it in tests and game days.
- **Deep health checks.** A probe that checks the database fails on every pod at once when the database blips, and the load balancer removes the whole fleet. See [Graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).
- **Bulkhead with an unbounded queue.** It isolates nothing; stalled calls just become memory growth and latency.
- **Failover flapping.** Require several missed heartbeats and add hysteresis.

## Interview questions

**Why is a slow dependency worse than a dead one?** A dead one fails fast. A slow one holds threads, connections and memory for the full timeout, so the caller runs out of capacity and fails unrelated requests. Timeouts, breakers and bulkheads bound that.

**Walk through circuit breaker states.** Closed records outcomes in a sliding window; once the failure rate crosses a threshold (after a minimum number of calls) it opens and fails fast. After a cool-down it goes half-open and allows a few probes; success closes it, failure reopens it.

**Circuit breaker versus bulkhead?** A breaker stops calling a dependency that looks unhealthy. A bulkhead limits how much of your capacity any dependency can hold, whether or not it looks unhealthy. You want both: the bulkhead protects you during the seconds before the breaker trips.

**What is a cell-based architecture and what does it cost?** Independent full-stack copies, each owning a subset of tenants behind a thin router. It caps blast radius and per-cell scale, at the price of cross-cell queries, tenant migrations and operating many copies.

**Explain shuffle sharding.** Each customer gets a random k-of-n subset of workers, so two customers rarely share an identical subset. A poison customer takes down only the few who share its full subset; others retry on their unaffected workers.

## Further reading

- *Release It! Design and Deploy Production-Ready Software* (Michael Nygard, 2nd ed.), the stability patterns chapters.
- *Site Reliability Engineering* (Beyer et al.), chapters "Handling Overload" and "Addressing Cascading Failures".
- AWS Builders' Library: "Workload isolation using shuffle-sharding" and "Static stability using Availability Zones".
- AWS Well-Architected whitepaper "Reducing the Scope of Impact with Cell-Based Architecture".
- Resilience4j documentation, CircuitBreaker and Bulkhead modules.
