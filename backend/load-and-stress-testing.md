---
title: Load and stress testing
section: backend
order: 60
phase: "Phase 8 · Performance and reliability"
summary: Model realistic workloads, avoid closed-loop and coordinated-omission traps, find breaking points with stress, soak and spike tests, and plan capacity.
---

A load test answers questions you otherwise learn during an incident: how many requests per second one pod sustains within the latency SLO, what breaks first when traffic doubles, whether the system recovers after a spike, and whether memory creeps up over a day. The tooling is easy; the hard part is a workload that resembles production and measurements that don't lie. A badly modeled test produces confident numbers that are off by an order of magnitude.

## Load testing

### Goals

| Question | Test |
|---|---|
| Does the script, data and environment work? | Smoke: 1–5 users, a few minutes |
| Do we meet the SLO at expected peak? | Load: expected peak for 15–60 minutes |
| Did this release regress latency or throughput? | The same load test in CI or pre-release, compared with a baseline |
| How much can one instance sustain? | Breakpoint: stepwise ramp until the SLO breaks |
| Where and how do we break, and do we recover? | Stress |
| Does autoscaling keep up with a sudden jump? | Spike |
| Do leaks or slow degradation appear over time? | Soak: normal load for hours |

Define pass/fail before running: "p99 below 300 ms and error rate below 0.5% at 1,200 RPS for 30 minutes" is a test. "See how it does" is a demo. Tie thresholds to your SLOs (see [Performance monitoring]({{ '/backend/performance-monitoring/' | relative_url }})).

### Workload modeling

A realistic workload reproduces what production traffic does to your bottlenecks:

- **Endpoint mix.** Take the ratio from access logs or APM: say 70% list and detail reads, 20% searches, 8% writes, 2% exports. Weight tasks to match.
- **Data distribution.** Tenants are skewed: a few large tenants produce most rows and the slowest queries. Pick ids with a Zipf-like distribution, not uniformly, and never one hard-coded id (100% cache hits) or one hot row (artificial lock contention).
- **Data volume.** Run against a database of production size and shape. A 10,000-row staging table keeps every index in memory and makes every plan look fine.
- **Sessions and think time.** Real users log in, browse, pause. Include authentication, token refresh and realistic pauses if you use a user-based model.
- **Payload sizes.** Uploads, large JSON bodies, long search strings.
- **Background load.** Celery jobs, cron rollups and replication run during real peaks too.
- **Caches and CDN.** Decide deliberately whether you are testing warm or cold caches; both matter.

### Open vs closed workload models

```text
 Closed model: N virtual users        Open model: arrivals at rate R
 +------+  send   +--------+          arrivals --> [ queue ] --> server
 | user | ------> | server |          (independent of response time)
 |  x N | <------ |        |
 +------+  reply  +--------+          slow server => queue grows,
 next request only after reply        latency grows, as in production
 slow server => fewer requests sent
```

- **Closed:** a fixed number of virtual users, each sending a request, waiting for the response, thinking, and repeating. Throughput is roughly `users / (response time + think time)`, so when the server slows down, the generator automatically sends less. The test is self-limiting and hides overload. Closed models fit systems with a fixed client population, such as a pool of internal workers.
- **Open:** requests arrive at a target rate regardless of how fast earlier ones complete. That is how internet traffic behaves: a new visitor doesn't wait for someone else's page to finish loading. Under overload the queue and latency grow without bound, which is what you need to observe.

Schroeder, Wierman and Harchol-Balter's "Open Versus Closed: A Cautionary Tale" shows that the two models can give very different answers for the same system. In k6, `constant-vus` and `ramping-vus` are closed; `constant-arrival-rate` and `ramping-arrival-rate` are open, and k6 reports `dropped_iterations` when it runs out of virtual users to maintain the rate. Locust is user-based (closed); `constant_throughput` paces each user but still waits for responses.

### Coordinated omission

Gil Tene named the most common measurement bug in load testing. A closed-loop generator sends a request, waits, then sends the next. If the server freezes for one second, the generator records **one** slow request, then politely resumes. The 100 requests that real users would have sent during that second are never sent, so their latency is never recorded. The tool has coordinated with the system under test to omit the bad samples, and p99 looks excellent.

Fixes:

- Use an open model (arrival-rate executors, wrk2) so requests are scheduled independently of responses.
- Measure latency from the **intended** send time, not the actual one. HdrHistogram can correct recorded data when you know the expected interval.
- Cross-check with server-side histograms. If the server's p99 is far worse than the generator's, the generator is lying.

The runnable demo below simulates a one-second stall and measures it both ways.

### Locust and k6

| | Locust | k6 |
|---|---|---|
| Scripts in | Python | JavaScript |
| Model | User-based (closed), pacing helpers | Executors for both closed and open models |
| Scale-out | Master + workers | Single binary scales far per machine; cloud or operator for distributed |
| Pass/fail | Custom checks or exit-code settings | Built-in `thresholds` fail the run |
| Fits | Python teams, complex user flows | CI gates, arrival-rate testing |

Both are mature; pick by team language and whether you need first-class arrival-rate scheduling. Gatling, JMeter, Vegeta and wrk2 are other common choices.

## Stress testing

### Finding the breaking point

```text
 p99
 latency |                                        *
         |                                      *
         |                                   *     past the knee:
         |                               *         queues grow and
         |                          *              latency explodes
         |  *    *    *    *    *
         +-------------------------------------------- throughput
                                ^ knee          ^ collapse
         operate left of the knee, with headroom
```

Ramp in steps (for example +15% every five minutes) and hold each step long enough to reach steady state. Throughput rises with load until the first resource saturates. Past that **knee**, extra load only adds queueing, and latency climbs steeply. Record at each step:

- RED per endpoint: rate, errors, duration percentiles.
- USE per resource: CPU, memory, DB connections in use and pool wait time, queue depth, thread pool saturation, file descriptors, network.

The first resource to saturate is your bottleneck. Common culprits in Django stacks: database connections and pool waits, a single hot query, gunicorn worker count, CPU throttling from container limits, and Redis or a third-party rate limit. Fix it or document it, then repeat to find the next.

Then remove the load and watch **recovery**. A healthy system returns to baseline latency within a minute or two. A system stuck in overload after the trigger is gone, usually because client retries keep it saturated, has a metastable failure. That is a finding as important as the peak number; see [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}). The behavior you want under overload is fast rejection (`429`/`503` via load shedding and [Backpressure]({{ '/backend/backpressure/' | relative_url }})), not timeouts for everyone.

### Soak tests

Hold normal-to-peak load for 4 to 24 hours. Soak tests catch what short tests can't: memory growth in workers, connection or file-descriptor leaks, log and disk growth, table and index bloat, queue backlog creep, token expiry in long sessions, and interactions with hourly cron jobs. The signal is a **trend**: latency or memory slowly climbing at constant load.

### Spike tests

Jump from baseline to 5–10× within seconds, then drop back. Spikes test what ramps don't:

- **Autoscaling lag.** Metrics scrape interval, the HPA sync period (15 s by default), scheduling, image pull, app startup and readiness add up to minutes, not seconds. Until new pods are ready, existing ones absorb the spike. See [Kubernetes]({{ '/backend/kubernetes/' | relative_url }}).
- **Cold paths.** New pods have empty local caches and connection pools; a burst of simultaneous misses can stampede the database (see [Cache stampede]({{ '/backend/cache-stampede/' | relative_url }})).
- **Connection storms.** Fifty new pods each opening their pools at once can exceed `max_connections`.

Known spikes (a product launch, a scheduled campaign email) are better handled by pre-scaling than by trusting the autoscaler.

### Capacity planning

Capacity planning turns measurements into instance counts. With a breakpoint test showing one pod sustains 180 RPS within the SLO:

```text
 forecast peak     = 2,400 RPS now x 1.5 growth (12 months) = 3,600 RPS
 usable per pod    = 180 RPS x 0.7 target utilization       =   126 RPS
 pods for peak     = 3,600 / 126 = 28.6                     ->   29 pods
 survive 1 of 3 AZ = 29 x 3 / 2  = 43.5                     ->   44 pods
 downstream check  = 44 pods x 3 workers x 8 threads = 1,056 DB conns
                     -> needs a pooler; see connection pooling
```

The utilization target leaves headroom for spikes, deploys (a rolling update removes capacity) and noisy neighbors. Repeat the measurement after major releases, since per-pod throughput drifts. Little's law (concurrency = throughput × latency) and Neil Gunther's Universal Scalability Law help predict how throughput degrades as you add instances that contend on shared resources.

## In practice

### Locust user flow (illustrative)

```python
import random
from locust import HttpUser, between, task

TENANTS = [f"tenant-{i}" for i in range(1, 201)]
WEIGHTS = [1 / i for i in range(1, 201)]        # Zipf-like: a few big tenants


class ApiUser(HttpUser):
    wait_time = between(1, 3)                     # think time (closed model)

    def on_start(self):
        tenant = random.choices(TENANTS, weights=WEIGHTS)[0]
        r = self.client.post("/api/token/", json={"tenant": tenant, "user": "load"})
        self.client.headers["Authorization"] = f"Bearer {r.json()['access']}"

    @task(10)
    def list_orders(self):
        self.client.get("/api/orders/?limit=50", name="/api/orders/")

    @task(3)
    def order_detail(self):
        oid = random.randint(1, 50_000)
        self.client.get(f"/api/orders/{oid}/", name="/api/orders/:id")

    @task(1)
    def create_order(self):
        self.client.post("/api/orders/", json={"sku": "A-1", "qty": 1})
```

```bash
locust -f locustfile.py --headless --users 2000 --spawn-rate 50 \
       --run-time 30m --host https://staging.example.com --csv results
```

The `name=` argument groups URLs with ids into one stats row, so you see one percentile line for `/api/orders/:id` instead of 50,000.

### k6 open-model test with thresholds (illustrative)

```javascript
import http from 'k6/http';
import { check } from 'k6';

export const options = {
  scenarios: {
    api: {
      executor: 'ramping-arrival-rate',     // open model: arrivals don't wait
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 200,
      maxVUs: 2000,
      stages: [
        { target: 500, duration: '5m' },    // ramp to expected peak
        { target: 500, duration: '20m' },   // hold: load test
        { target: 1500, duration: '10m' },  // push past it: stress
      ],
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<300', 'p(99)<800'],
    http_req_failed: ['rate<0.01'],
    dropped_iterations: ['count<100'],      // generator kept up with the rate
  },
};

export default function () {
  const res = http.get(`${__ENV.BASE_URL}/api/orders/?limit=50`, {
    headers: { Authorization: `Bearer ${__ENV.TOKEN}` },
  });
  check(res, { 'status is 200': (r) => r.status === 200 });
}
```

### Coordinated omission and capacity math, runnable

```python
import math


def finish(start, service, stall):
    """A single FIFO server that is frozen during [stall[0], stall[1])."""
    if stall[0] <= start < stall[1]:
        start = stall[1]
    return start + service


def simulate(duration=20_000, interval=10, service=1, stall=(10_000, 11_000)):
    """Times in ms. One request every `interval` ms; the server stalls for 1 s."""
    closed, t = [], 0
    while t < duration:                           # closed loop: wait for each reply
        done = finish(t, service, stall)
        closed.append(done - t)                   # measured from the actual send
        t = max(t + interval, done)

    corrected, server_free = [], 0
    for intended in range(0, duration, interval):   # open loop: fixed schedule
        start = max(intended, server_free)
        done = finish(start, service, stall)
        server_free = done
        corrected.append(done - intended)         # measured from the intended send
    return closed, corrected


def percentile(samples, p):
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def pods_needed(peak_rps, per_pod_rps, target_util=0.7, zones=3):
    pods = math.ceil(peak_rps / (per_pod_rps * target_util))
    return math.ceil(pods * zones / (zones - 1))  # survive losing one zone


if __name__ == "__main__":
    closed, corrected = simulate()
    for label, s in [("closed loop", closed), ("corrected", corrected)]:
        print(f"{label:12} n={len(s):5} mean={sum(s) / len(s):6.1f} ms "
              f"p50={percentile(s, 50):4} p99={percentile(s, 99):4} max={max(s):5}")
    assert percentile(closed, 99) == 1            # the stall vanishes from p99
    assert len(closed) < len(corrected)           # requests that were never sent
    assert percentile(corrected, 99) > 500        # the users' real experience
    assert max(closed) == max(corrected) == 1001  # both saw the worst request

    assert pods_needed(peak_rps=3600, per_pod_rps=180) == 44
```

## Pitfalls

- **The load generator is the bottleneck.** Generator CPU at 100% caps throughput and inflates latency. Monitor the generators, and scale them out (Locust workers, more k6 instances).
- **Unrealistic data.** Small databases, one test user, uniform ids, or an always-warm cache produce numbers production will never see.
- **Fast errors look like good latency.** A service returning `500` in 2 ms improves the latency chart. Always assert status codes and gate on error rate.
- **Averages and closed loops.** Report percentiles from histograms and use open models for internet-facing services. Check for coordinated omission by comparing with server-side metrics.
- **No warm-up.** The first minutes include connection setup, cache fills and autoscaling. Exclude them or report them separately.
- **Hitting real third parties.** Load tests that charge cards, send emails or exhaust a partner's rate limit cause real damage. Stub external services at the boundary.
- **Blocked by your own defenses.** Rate limiters and WAFs throttle the generator's IPs. Allowlist them deliberately, or test the limiter on purpose.
- **Unannounced tests against shared environments.** Coordinate with on-call; a stress test looks exactly like an incident.

## Interview questions

**What's the difference between load, stress, soak and spike testing?**
Load tests check behavior at expected peak against SLOs. Stress tests push beyond it to find the breaking point and failure mode. Soak tests hold normal load for hours to expose leaks and slow degradation. Spike tests apply a sudden jump to test autoscaling, cold caches and connection storms.

**Open vs closed workload model: which for a public API?**
Open. Internet users arrive independently of how fast you respond, so arrivals should follow a rate, and overload should show up as growing queues. A closed model throttles itself when the system slows and hides the overload.

**What is coordinated omission?**
A measurement bias where the load generator waits on slow responses and so doesn't send the requests that would have hit the slow period. Their latency is never recorded and tail percentiles look far better than reality. Fix it with arrival-rate scheduling and measuring from intended send time.

**How do you decide how many instances you need?**
Measure the maximum throughput per instance within the SLO with a breakpoint test. Divide forecast peak by that number times a target utilization (say 70%), then add redundancy for losing a zone. Check that downstream limits like DB connections can absorb the total.

**Your load test shows p99 of 200 ms but production p99 is 2 s. Why?**
Likely culprits are an unrealistic workload (warm cache, uniform keys, small database), coordinated omission or a closed model hiding queueing, a different endpoint mix, missing background jobs, or noisy neighbors and CPU throttling in production. Compare server-side histograms from both.

**What do you monitor during a stress test?**
RED metrics per endpoint and USE metrics per resource: CPU, memory, connection pool usage and wait time, queue depth, thread saturation, database load. Also watch the generators. The first resource that saturates is the bottleneck.

**How do you load test a flow that calls a payment provider?**
Stub the provider at the HTTP boundary with a fake that has realistic latency and error rates, or use the provider's sandbox within its published limits. Never generate real charges, and test your timeout and retry behavior against the stub's slow and failing modes.

## Further reading

- Bianca Schroeder, Adam Wierman and Mor Harchol-Balter, "Open Versus Closed: A Cautionary Tale" (NSDI 2006).
- Gil Tene, "How NOT to Measure Latency" (talk), plus the HdrHistogram and wrk2 documentation on coordinated omission.
- k6 documentation on scenarios, executors and thresholds; the Locust documentation.
- Neil J. Gunther, *Guerrilla Capacity Planning* (Universal Scalability Law).
- Nathan Bronson et al., "Metastable Failures in Distributed Systems" (HotOS 2021).
