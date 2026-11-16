---
title: Backpressure
section: backend
order: 47
phase: "Phase 6 · Messaging and events"
summary: Keep fast producers from drowning slow consumers with bounded queues, block/drop/buffer policies, load shedding, credit-based flow control and lag autoscaling.
---

Every pipeline has a slowest stage, and a queue in front of it does not make it faster; it only decides where the pile forms. If producers outpace consumers for long enough, an unbounded queue grows until memory runs out or latency becomes useless, and then the process dies and takes every in-flight item with it. Backpressure is the signal that travels from the slow stage back to the fast one: slow down, or some work will be refused. The design question is never whether to have it, only where it happens and what gets dropped.

Little's law makes the arithmetic concrete: items in the system `L = λ × W` (arrival rate times time in system). If arrivals are 1,000/s and the consumer handles 800/s, the backlog grows by 200 per second: 12,000 after a minute, 720,000 after an hour, with the wait for a new item growing linearly the whole time.

```text
 producer --(1000/s)--> [ queue ] --(800/s)--> consumer
                            |
             grows 200/s: 12k after 1 min, 720k after 1 h
                            |
        +-------------------+--------------------+
        |                   |                    |
      block           drop or shed         buffer and scale
 (slow the producer) (refuse some work) (absorb bursts, add consumers)
```

## Backpressure handling

### Bounded queues

An unbounded queue hides overload until it becomes an outage. A bounded queue makes overload visible at the moment it starts and forces an explicit policy for the "full" case.

Sizing has two inputs:

- **Burst absorption.** Capacity of at least (burst arrival rate minus service rate) times burst duration, so normal spikes never hit the limit.
- **Latency budget.** Maximum useful queue length is service rate times the wait a caller will tolerate. At 800/s and a 2 s budget, anything past 1,600 items will time out upstream anyway; queuing it is wasted memory and wasted work.

Unbounded queues hide in defaults: `asyncio.Queue()` (maxsize 0), `concurrent.futures.ThreadPoolExecutor` (its internal work queue is unbounded, so `submit` never blocks), client-library send buffers, Celery workers prefetching tasks, and in-memory lists in "temporary" batching code. Find them and give them limits.

### Blocking vs dropping vs buffering

| Policy | Producer sees | Data loss | Latency | Use for |
|---|---|---|---|---|
| Block | `put()` waits; TCP window closes | None | Moves upstream | Pipelines where upstream can wait (ETL, internal workers) |
| Drop newest (reject) | Error: 429/503, `queue.Full` | New work | Bounded | Request/response APIs; client retries later |
| Drop oldest | Nothing | Stale work | Bounded, fresh | Telemetry, live prices, location updates |
| Coalesce | Nothing | Intermediate values | Bounded | Latest value per key (dashboards, cache refresh) |
| Buffer to durable log | Nothing (until disk/retention) | None until retention | Grows | Absorbing long bursts with Kafka or SQS |

Blocking is the purest backpressure because it propagates: a blocked producer stops reading its own input, its upstream fills, and the slowdown reaches the source. It fails badly when the producer is an event loop (one blocked `put` stalls every coroutine), when producer and consumer depend on each other's queues (deadlock), or when upstream has a timeout shorter than the block. The Kafka producer is a good example of bounded blocking: when `buffer.memory` is full, `send()` blocks for up to `max.block.ms` and then raises.

Buffering into a durable log does not remove the need for backpressure; it moves it. Kafka absorbs hours of backlog, but the consumer's lag still grows, and once lag passes retention, data is deleted unread.

### Load shedding

Load shedding is dropping at the front door on purpose. Rejecting a request in 1 ms is far cheaper than accepting it, doing 300 ms of work, and timing out anyway, which is what an overloaded service does to every request at once.

Signals to shed on:

- **Concurrency.** In-flight requests above a limit (the cheapest and most robust signal).
- **Queue wait time.** If a request already waited longer than the client's timeout, drop it before doing any work. CoDel-style shedding drops when queue sojourn time stays above a target.
- **Deadline propagation.** Pass the remaining deadline downstream (gRPC does this natively) and skip work whose caller has already given up.
- **Priority.** Shed batch, prefetch and background traffic before interactive traffic; shed anonymous before paying tenants.

Return 503 or 429 with `Retry-After`, and make clients back off with jitter and respect a retry budget. Retries are the main way overload turns into a cascade: three layers each retrying three times turn one failed request into 27 calls. See [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}) and [Rate limiting]({{ '/backend/rate-limiting/' | relative_url }}).

Static limits drift as code and hardware change. Adaptive concurrency limits measure latency and adjust the limit with AIMD or gradient algorithms, borrowing from TCP congestion control. Netflix publicly described and open-sourced a concurrency-limits library built on this idea, and Envoy has an adaptive concurrency filter.

### Rate-based vs credit-based flow control

| | Rate-based | Credit-based (windowed) |
|---|---|---|
| Mechanism | Sender limited to N/s (token bucket) | Receiver grants N credits; sender sends at most N, receiver replenishes as it finishes |
| Loop | Open: configured, maybe tuned from metrics | Closed: adapts to actual consumer speed |
| Fails when | Consumer slows (GC, noisy neighbor); rate is now too high | Credits lost or receiver stalls (sender waits) |
| Examples | API rate limits, producer throttles, `limit_req` in nginx | TCP receive window, HTTP/2 and gRPC `WINDOW_UPDATE`, AMQP `basic.qos` prefetch, AMQP 1.0 link credit, Reactive Streams `request(n)` |

Pull-based consumers (Kafka, SQS, Redis Streams) are implicitly credit-based: the consumer asks for more only when it is ready, so the broker never overwhelms it. The backpressure shows up as lag instead. Push-based delivery (RabbitMQ, webhooks, WebSockets) needs an explicit window; in RabbitMQ that is the prefetch count, and without it the broker pushes the whole queue into the consumer's memory.

### Reactive streams

Reactive Streams is a small specification (four interfaces: `Publisher`, `Subscriber`, `Subscription`, `Processor`) whose core rule is that a publisher never emits more items than the subscriber requested through `Subscription.request(n)`. That makes demand a first-class signal through every stage of an asynchronous pipeline. Java 9 adopted it as `java.util.concurrent.Flow`; Project Reactor (Spring WebFlux), RxJava, and Akka/Pekko Streams implement it, with operators like `onBackpressureBuffer`, `onBackpressureDrop` and `onBackpressureLatest` that map onto the policies in the table above.

Python has no standard equivalent, but the same ideas apply: async generators are pull-based by construction, `asyncio.Queue(maxsize=n)` blocks producers at `await put()`, and stream writers expose the transport's buffer through `await writer.drain()`, which waits until the kernel accepted the data. Backpressure across a network hop needs protocol support (TCP windows, HTTP/2 flow control, or explicit credits); an in-process `request(n)` does not cross a socket by itself.

### Consumer lag autoscaling

Autoscaling is the slow loop of backpressure: when lag persists, add consumers. Scale on a measure of time, not raw count, because 50,000 messages is ten seconds of work for one consumer and an hour for another:

```text
 desired_replicas = ceil(lag / (per_replica_rate × target_drain_seconds))
 e.g. lag 600,000, 500 msg/s per replica, drain in 120 s -> 10 replicas
```

Constraints:

- **Kafka caps parallelism at the partition count.** Consumers beyond it sit idle. Overprovision partitions if you plan to scale out.
- **Scaling moves the bottleneck.** Twenty consumers hammering one Postgres instance turn queue lag into connection exhaustion and lock contention. Cap replicas at what the downstream can take (see [Connection pooling]({{ '/backend/connection-pooling/' | relative_url }})).
- **It takes minutes.** Pods schedule, warm up and rebalance. Bounded queues and shedding handle the first minutes; autoscaling handles the next hour.
- **Flapping.** Use a scale-down stabilization window; a rebalance on every scale event costs throughput.

KEDA is the usual Kubernetes answer: it reads lag or queue depth and drives an HPA. Illustrative:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: order-consumer
spec:
  scaleTargetRef:
    name: order-consumer          # the Deployment
  minReplicaCount: 2
  maxReplicaCount: 24             # <= partition count, <= what the DB can take
  cooldownPeriod: 300
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka:9092
        consumerGroup: order-consumer
        topic: orders
        lagThreshold: "5000"      # target lag per replica
```

For SQS the `aws-sqs-queue` scaler uses queue length; alert separately on `ApproximateAgeOfOldestMessage`, which is the latency your users feel.

## In practice

### Runnable: bounded queue policies

A discrete-time simulation shows the four policies with a producer at 10 items per tick and a consumer at 6, then a real threaded pipeline where `queue.Queue(maxsize=10)` blocks the producer and keeps memory flat.

```python
import queue
import threading
import time
from collections import deque


def simulate(policy, ticks=60, produce=10, consume=6, capacity=20):
    """Discrete-time pipeline: producer offers 10/tick, consumer drains 6/tick."""
    buf, dropped, produced, peak, seq = deque(), 0, 0, 0, 0
    for _ in range(ticks):
        for _ in range(produce):
            if policy != "unbounded" and len(buf) >= capacity:
                if policy == "block":
                    break                 # producer stalls until next tick
                if policy == "drop_newest":
                    dropped += 1          # reject: 429/503 upstream
                    seq += 1
                    continue
                if policy == "drop_oldest":
                    buf.popleft()         # keep the freshest data
                    dropped += 1
            buf.append(seq)
            seq += 1
            produced += 1
        peak = max(peak, len(buf))
        for _ in range(min(consume, len(buf))):
            buf.popleft()
    return {"peak": peak, "dropped": dropped, "accepted": produced,
            "newest_kept": buf[-1] if buf else None}


def blocking_pipeline(n_items=300, maxsize=10):
    """Real threads: put() blocks when the queue is full, so memory stays bounded."""
    q, done, peak = queue.Queue(maxsize=maxsize), [], [0]

    def consumer():
        while (item := q.get()) is not None:
            time.sleep(0.0005)            # slower than the producer
            done.append(item)

    t = threading.Thread(target=consumer)
    t.start()
    for i in range(n_items):
        q.put(i)                          # blocks: this is the backpressure
        peak[0] = max(peak[0], q.qsize())
    q.put(None)
    t.join()
    return done, peak[0]


if __name__ == "__main__":
    results = {p: simulate(p) for p in ("unbounded", "block", "drop_newest", "drop_oldest")}
    for name, r in results.items():
        print(f"{name:12} {r}")

    assert results["unbounded"]["peak"] == 60 * (10 - 6) + 6   # grows every tick
    for p in ("block", "drop_newest", "drop_oldest"):
        assert results[p]["peak"] <= 20                          # bounded memory
    assert results["block"]["dropped"] == 0                      # no loss, slower intake
    assert results["block"]["accepted"] < results["unbounded"]["accepted"]
    assert results["drop_newest"]["dropped"] > 0
    assert results["drop_oldest"]["newest_kept"] == 60 * 10 - 1  # freshest survives

    done, peak = blocking_pipeline()
    assert done == list(range(300)) and peak <= 10
    print("threaded: all 300 delivered in order, peak queue", peak)
```

The unbounded queue ends at 246 items and would keep growing; every bounded policy stays at 20. Blocking loses nothing, but the producer managed only 374 items in the time it wanted to send 600: it was slowed to the consumer's pace, which is the point. Drop-newest and drop-oldest each discard 226 items; the difference is that drop-oldest keeps the most recent data.

### Pushing back in a Django and Celery stack (illustrative)

```python
# settings.py: Celery reads CELERY_* settings via config_from_object(namespace="CELERY")
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # each worker process reserves one task
CELERY_TASK_ACKS_LATE = True            # unfinished tasks return to the broker

# pika consumer: credit-based window instead of the whole queue in memory
channel.basic_qos(prefetch_count=50)
```

An in-process concurrency limit sheds load before any expensive work, for threaded (gthread) or ASGI deployments:

```python
import threading

from django.http import JsonResponse

_slots = threading.BoundedSemaphore(64)   # per process; size it from load tests


class ShedLoadMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _slots.acquire(blocking=False):
            return JsonResponse({"error": "overloaded"}, status=503,
                                headers={"Retry-After": "2"})
        try:
            return self.get_response(request)
        finally:
            _slots.release()
```

Put it first in `MIDDLEWARE` so a rejected request costs no session lookup or database query. With sync workers, the hidden queue is the listen backlog in front of Gunicorn; shed at the proxy (nginx `limit_conn`, an Envoy circuit breaker) instead.

## Pitfalls

- **Unbounded defaults.** `ThreadPoolExecutor.submit` and `asyncio.Queue()` accept work forever. Symptom: memory climbs during load tests and p99 latency climbs with it. Wrap with a semaphore or set `maxsize`.
- **Blocking inside an event loop.** A synchronous `queue.put()` in async code freezes every request in the process. Use `await asyncio.Queue.put()` or `put_nowait()` with an explicit drop.
- **Shedding too late.** Rejecting after authentication, parsing and a database read saves little. Shed at the edge, first.
- **Retry storms.** Clients that retry immediately multiply load exactly when capacity is lowest. Backoff, jitter, retry budgets, and honoring `Retry-After`.
- **Silent drops.** Dropping without a counter is data loss nobody can see. Emit `dropped_total` by reason and alert on it.
- **Measuring depth, not wait.** A queue of 10,000 can be fine or terrible. Track queue wait (time from enqueue to start) as a histogram.
- **Autoscaling into the database.** Consumers scale to 50, Postgres hits `max_connections`, everything slows. Cap replicas and pool sizes together.
- **Lag past retention.** A Kafka consumer that falls behind by more than retention loses data with `auto.offset.reset` deciding where it resumes. Alert on lag in time, well below retention.
- **Cyclic bounded queues.** Stage A waits on B's full queue while B waits on A's. Keep pipelines acyclic or make one edge drop instead of block.

## Interview questions

**What is backpressure and how is it different from rate limiting?** Backpressure is a feedback signal from a slow consumer that makes producers slow down or shed work, sized by the consumer's actual capacity. Rate limiting is a fixed policy on how much a client may send, regardless of current capacity. Rate limits protect fairness and cost; backpressure protects stability.

**A queue keeps growing. What do you do?** Short term: shed or deprioritize low-value producers, scale consumers if the downstream can absorb it, make sure nothing is retrying aggressively. Then find whether arrival rose or service rate fell (a slow query, a dependency, a bad deploy). Long term: capacity plan, bound the queue, and alert on wait time.

**Block or drop?** Block when upstream can wait and every item matters (internal pipelines, ETL). Drop when the caller can retry or newer data supersedes older (APIs return 429/503; telemetry drops oldest). Never block an event loop or a user-facing request thread indefinitely.

**How does Kafka handle backpressure?** Consumers pull at their own pace, so the broker never overwhelms them; overload becomes consumer lag. On the producer side, a full `buffer.memory` blocks `send()` up to `max.block.ms`. The risk is lag exceeding retention, so monitor lag in seconds and scale consumers up to the partition count.

**Give examples of credit-based flow control.** TCP receive windows, HTTP/2 and gRPC stream windows, RabbitMQ prefetch (`basic.qos`), AMQP 1.0 link credit, Reactive Streams `request(n)`. The receiver advertises how much it can take; the sender never exceeds it.

**How do you autoscale queue consumers safely?** Scale on lag converted to drain time or on oldest-message age, cap replicas at the partition count and at what downstream dependencies can handle, and use stabilization windows to avoid rebalance churn. Keep bounded queues and shedding for the minutes before new replicas are ready.

**How do you stop overload from cascading across services?** Bounded queues and concurrency limits per dependency (bulkheads), timeouts with deadline propagation, circuit breakers, early load shedding, and retry budgets so callers back off instead of multiplying load. See [Fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}).

## Further reading

- Reactive Streams specification (reactive-streams.org) and the `java.util.concurrent.Flow` API documentation
- Google, *Site Reliability Engineering*: "Handling Overload" and "Addressing Cascading Failures"
- Amazon Builders' Library: "Using load shedding to avoid overload" and "Avoiding insurmountable queue backlogs"
- Kathleen Nichols and Van Jacobson, "Controlling Queue Delay" (ACM Queue, 2012)
- KEDA documentation: Apache Kafka and AWS SQS Queue scalers
