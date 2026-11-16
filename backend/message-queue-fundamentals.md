---
title: Message queues and pub/sub
section: backend
order: 41
phase: "Phase 6 · Messaging and events"
summary: Why queues exist, how acks, visibility timeouts, prefetch and competing consumers work, queues versus logs, and pub/sub fan-out with filtering.
---

When a signup request has to create the account, send a welcome email, provision a workspace, sync to the CRM and update analytics, doing all of that inline makes the request as slow as the slowest step and as available as the least available service. A message broker lets the request do the one thing that must happen now, write a message, and return. Workers do the rest at their own pace, retry on their own schedule, and can be down for a while without the user noticing. That decoupling is the whole point, and it comes with a bill: duplicates, reordering, eventual consistency and a new piece of infrastructure to run. This note covers the core model shared by every broker. Product-specific details are in [Kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }}) and [RabbitMQ and SQS]({{ '/backend/rabbitmq-and-sqs/' | relative_url }}).

## Message queue fundamentals

### Why queues

| Benefit | What it means in practice |
|---|---|
| Temporal decoupling | Producer and consumer don't need to be up at the same time |
| Load leveling | A burst of 50,000 jobs drains at the rate workers can sustain |
| Independent scaling | Add consumers without touching producers |
| Failure isolation | A crashing email worker doesn't fail signups |
| Retries off the request path | Failed work is retried later, not while a user waits |
| Fan-out | One event reaches many independent consumers |

The costs: the producer no longer knows if or when the work succeeded, so you need status tracking and idempotent consumers; messages can be delivered more than once and out of order; and debugging crosses process boundaries, which is why trace context should travel in message headers. Use a queue when the caller does not need the result to respond. Keep the call synchronous when it does, for example checking stock before confirming an order.

If you already run PostgreSQL and need modest throughput, a table with `SKIP LOCKED` is a legitimate queue and gives you transactional enqueue for free; see [Postgres as a job queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }}).

### Point-to-point queues

In a point-to-point queue each message is processed by exactly one consumer. This is the work queue or task queue: resize this image, send this email, generate this invoice. Celery, Sidekiq, SQS and a RabbitMQ queue with several consumers all follow this model.

A message moves through a small lifecycle:

```text
 producer             broker                         consumer
    |  send  +---------+  deliver  +-----------+
    +------> |  ready  | --------> | in flight | ----> process
             +---------+           +-----------+          |
                  ^                                       |
                  |  lease timeout, nack, or              |
                  +--- consumer connection lost ----------+
                                                          |
                    ack: broker deletes the message <-----+

 after N failed deliveries the broker moves it to a dead-letter queue
```

The broker hands a message out, marks it in flight, and deletes it only when the consumer acknowledges it. If the consumer dies or rejects the message, it goes back to ready and someone else gets it.

### Acknowledgments

The ack is the consumer telling the broker "done, you can forget this". When you send it decides your delivery guarantee:

| Ack timing | Crash before ack | Guarantee |
|---|---|---|
| Auto-ack on delivery | Message lost | At-most-once |
| Ack after processing | Message redelivered, maybe processed twice | At-least-once |
| Ack after processing, idempotent handler | Redelivery is harmless | Effectively once |

Ack after the side effects are durable, for example after the database transaction commits, never before. A negative ack (nack or reject) tells the broker processing failed; you choose whether to requeue it or send it to a dead-letter queue. Requeueing a message that will always fail creates a hot loop, so cap deliveries and dead-letter the rest. [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) and [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}) go deeper.

### Visibility timeouts and leases

How does the broker know a consumer died? Two models:

- **Connection-based (RabbitMQ).** Unacked messages stay assigned to the consumer's channel. If the connection closes, they are requeued and marked redelivered. A consumer that hangs while connected keeps its messages; RabbitMQ's consumer delivery timeout (30 minutes by default in recent versions) closes the channel eventually.
- **Lease-based (SQS, most database queues).** Receiving a message hides it for a visibility timeout. Delete it before the timeout and it is gone; otherwise it becomes visible again for another consumer. No connection state is needed, which suits HTTP APIs.

Set the visibility timeout above the p99 processing time. Too short and a slow-but-alive worker's message is handed to a second worker, so two process it at once. Too long and a crashed worker's message waits that long before anyone retries. For jobs with unpredictable duration, use a moderate timeout and extend it periodically from a heartbeat while the work runs (`ChangeMessageVisibility` in SQS).

### Prefetch and flow control

Fetching one message per round trip wastes time on network latency, so consumers prefetch: the broker pushes up to N unacked messages to each consumer ahead of time. RabbitMQ calls this the QoS prefetch count, SQS lets one receive call return up to 10 messages, and Celery's `worker_prefetch_multiplier` (default 4) multiplies by worker concurrency.

| Prefetch | Good for | Risk |
|---|---|---|
| 1 | Long, uneven tasks | Lower throughput; round trip per message |
| Tens to a few hundred | Short, uniform messages | One slow message holds others hostage |
| Unlimited | Nothing | A consumer hoards the queue and runs out of memory |

The classic failure: a worker prefetches 16 messages, the first takes 10 minutes, and the other 15 sit behind it while idle workers have nothing to do. For long tasks use prefetch 1 with late acks.

### Competing consumers

Several consumers reading the same queue split the work between them. Throughput scales by adding consumers until the broker or a downstream dependency (usually the database) becomes the bottleneck. Two consequences:

- **Ordering is lost.** Messages 1 and 2 go to different workers; 2 can finish first. If order matters per entity, route by key to a partition or message group (see [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }})).
- **Autoscale on the queue, not CPU.** Scale on queue depth divided by per-worker throughput, or on the age of the oldest message (`ApproximateAgeOfOldestMessage` in SQS), which maps directly to how late the work is. KEDA does this on Kubernetes.

### Queue versus log

Brokers fall into two families, and mixing them up leads to bad designs:

| | Queue (RabbitMQ, SQS, Celery) | Log (Kafka, Kinesis, Redis Streams) |
|---|---|---|
| After consumption | Message deleted on ack | Record stays until retention expires |
| Consumer state | Broker tracks each message | Consumer tracks an offset per partition |
| Replay | No | Yes: reset the offset |
| Ack granularity | Per message | Offset: "everything before here" |
| Parallelism | Any number of consumers | At most one consumer per partition per group |
| Ordering | Best effort (FIFO variants exist) | Strict within a partition |
| Slow message | Others proceed around it | Blocks its partition |
| Best for | Task distribution, per-message retries | Event streams, many readers, replay, high volume |

A queue is a to-do list: work disappears when done, and a failed item can be retried on its own. A log is a ledger: history stays, many readers each keep their own place, and a new consumer can read from the beginning. Jay Kreps's essay "The Log" explains why the log is the more general primitive for data integration.

## Publish-subscribe pattern

In pub/sub, a publisher sends a message to a topic without knowing who receives it, and every subscriber gets its own copy. The publisher states a fact ("order 812 was placed"), and billing, analytics and email each react to it. That is the basis of [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}).

```text
                         +--> [sub: billing queue]   --> 2 billing workers
 order-service           |
   publish  --> topic ---+--> [sub: email queue]     --> email worker
 "OrderPlaced"  orders   |
                         +--> [sub: eu-fraud queue]  --> fraud check
                              filter: region = "eu"

 every subscription gets every matching message;
 consumers within one subscription compete for it
```

### Topics and fan-out

A topic is a named channel. Fan-out is the broker copying each message to every subscription, so adding a consumer requires no change to the publisher. Combine the two models by giving each subscribing service its own queue: SNS topic to one SQS queue per service, a RabbitMQ fanout exchange to one queue per service, or one Kafka consumer group per service. Each service then gets its own retries, DLQ, scaling and backlog, so a slow analytics consumer never delays billing.

### Subscriptions

| Type | Behavior | Example |
|---|---|---|
| Ephemeral | Only receives while connected; missed messages are gone | Redis Pub/Sub, RabbitMQ exclusive auto-delete queue |
| Durable | Messages accumulate while the subscriber is down | SNS to SQS, durable RabbitMQ queue, Kafka consumer group |

Redis Pub/Sub is fire-and-forget: fine for cache invalidation hints or live UI updates, wrong for anything that must not be lost. Durable subscriptions need retention limits and monitoring, because a forgotten subscription keeps accumulating messages.

### Filtering

Without filtering every subscriber receives everything and discards most of it.

- **Topic hierarchy or routing keys**: RabbitMQ topic exchanges match keys like `order.eu.created` against patterns such as `order.*.created` or `order.#`.
- **Attribute or content filters**: SNS subscription filter policies, Google Pub/Sub subscription filters, and EventBridge rules match on message attributes or fields of the body.
- **Consumer-side filtering**: Kafka has no broker-side filter; the consumer reads and skips. Cheap per message, but you pay for the bandwidth, so split very different event types into separate topics.

Filter on stable, low-cardinality attributes (event type, region, tenant tier), and put them in headers or attributes so the broker does not parse the body.

## In practice

A runnable in-memory broker showing leases, acks, redelivery, a dead-letter list, competing consumers and filtered fan-out:

```python
import itertools


class Queue:
    """Point-to-point queue with SQS-style visibility timeouts."""

    def __init__(self, clock, visibility_timeout=30.0, max_receives=3):
        self.clock, self.vt, self.max_receives = clock, visibility_timeout, max_receives
        self.messages = {}           # id -> [body, visible_at, receive_count]; dicts keep order
        self.dead_letters = []
        self.ids = itertools.count()

    def send(self, body):
        self.messages[next(self.ids)] = [body, 0.0, 0]

    def receive(self, max_messages=1):
        now, batch = self.clock(), []
        for mid, msg in list(self.messages.items()):
            if len(batch) == max_messages:
                break
            if msg[1] > now:
                continue                                  # leased to another consumer
            if msg[2] >= self.max_receives:
                self.dead_letters.append(self.messages.pop(mid)[0])
                continue
            msg[1], msg[2] = now + self.vt, msg[2] + 1    # hide it and count the delivery
            batch.append((mid, msg[0]))
        return batch

    def ack(self, mid):
        self.messages.pop(mid, None)


class Topic:
    """Pub/sub: each subscription queue gets its own copy of matching messages."""

    def __init__(self):
        self.subscriptions = []

    def subscribe(self, queue, where=lambda message: True):
        self.subscriptions.append((queue, where))

    def publish(self, message):
        for queue, where in self.subscriptions:
            if where(message):
                queue.send(message)


if __name__ == "__main__":
    now = [0.0]
    clock = lambda: now[0]

    jobs = Queue(clock)
    for i in range(4):
        jobs.send(f"resize-{i}")

    worker_a, worker_b = jobs.receive(2), jobs.receive(2)       # competing consumers
    assert len(worker_a) == len(worker_b) == 2
    assert not {m for _, m in worker_a} & {m for _, m in worker_b}

    for mid, _ in worker_a:
        jobs.ack(mid)                       # A finishes; B crashes without acking
    assert jobs.receive(10) == []           # B's messages are still leased
    now[0] += 31
    assert sorted(m for _, m in jobs.receive(10)) == sorted(m for _, m in worker_b)

    now[0] += 31
    jobs.receive(10)                        # third failed delivery
    now[0] += 31
    assert jobs.receive(10) == [] and len(jobs.dead_letters) == 2

    orders = Topic()
    billing, email, eu_fraud = Queue(clock), Queue(clock), Queue(clock)
    orders.subscribe(billing)
    orders.subscribe(email)
    orders.subscribe(eu_fraud, where=lambda m: m["region"] == "eu")
    orders.publish({"order_id": 1, "region": "us"})
    orders.publish({"order_id": 2, "region": "eu"})
    assert len(billing.receive(10)) == len(email.receive(10)) == 2
    assert [m["order_id"] for _, m in eu_fraud.receive(10)] == [2]
    print("queue and pub/sub ok")
```

Celery settings for long, uneven jobs on Django (illustrative):

```python
# settings.py
CELERY_TASK_ACKS_LATE = True              # ack after the task returns, not on receipt
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # requeue if the worker process is killed
CELERY_WORKER_PREFETCH_MULTIPLIER = 1     # don't hoard messages behind a long task
# Redis/SQS transports emulate acks with a visibility timeout: keep it above
# the longest task and any countdown/ETA, or the task runs twice.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
```

Enqueue only after the transaction commits, or a worker can pick up the job before the row exists, or run a job for a rollback:

```python
from django.db import transaction


def signup(request):
    with transaction.atomic():
        user = User.objects.create(email=request.POST["email"])
        transaction.on_commit(lambda: send_welcome_email.delay(user.id))
```

`on_commit` still loses the message if the process dies between commit and publish. When that matters, write the event to an outbox table in the same transaction and relay it to the broker; see [Event-driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}).

## Pitfalls

- **Non-idempotent consumers.** Every broker redelivers. Symptom: duplicate emails or double-applied credits after a deploy. Fix: dedupe on a message or business ID.
- **Visibility timeout shorter than processing time.** Symptom: the same job running on two workers, receive counts climbing without errors. Fix: longer timeout or heartbeat extension.
- **Publishing inside a transaction that later rolls back,** or before commit. Fix: `on_commit` or an outbox.
- **Unbounded prefetch.** Memory growth and head-of-line blocking on one consumer while others idle.
- **No dead-letter queue.** A poison message is retried forever and burns a worker. Cap deliveries and alert on DLQ depth.
- **Huge payloads.** Brokers are tuned for small messages. Store the blob in object storage and send a reference (the claim-check pattern).
- **Monitoring depth only.** Depth without throughput says little. Alert on the age of the oldest message, which is the user-visible delay.
- **Using a queue where you needed a log.** If a new service later needs history, a queue has already deleted it.

## Interview questions

**Why put a queue between two services?** To decouple availability and latency, absorb bursts, retry failed work off the request path, and scale consumers independently. The cost is asynchronous, at-least-once, possibly reordered processing.

**What is a visibility timeout?** The lease a consumer gets on a received message. If it isn't deleted before the timeout, the message becomes visible again, which is how lease-based brokers recover from crashed consumers.

**How do acks affect delivery guarantees?** Ack before processing gives at-most-once; ack after processing gives at-least-once. Exactly-once effects need at-least-once delivery plus idempotent handling.

**What does prefetch control?** How many unacked messages the broker pushes to a consumer ahead of time. Higher helps throughput for short messages; 1 is right for long or uneven tasks.

**Queue versus log?** A queue deletes acknowledged messages and tracks each message, which suits task distribution. A log retains records, consumers track offsets, and it supports replay and many independent readers, with ordering and parallelism per partition.

**How do you implement pub/sub with per-consumer retries?** Fan out from a topic to one durable queue per consuming service (SNS to SQS, fanout exchange to queues, one consumer group per service). Each gets its own retries, DLQ and scaling.

**How do you scale consumers?** Add competing consumers and autoscale on backlog or oldest-message age, up to the limit of downstream dependencies. If you need per-key ordering, partition by key.

## Further reading

- *Enterprise Integration Patterns* (Gregor Hohpe and Bobby Woolf), the messaging channel and endpoint patterns.
- *Designing Data-Intensive Applications* (Martin Kleppmann), chapter 11, "Stream Processing".
- "The Log: What every software engineer should know about real-time data's unifying abstraction" (Jay Kreps, 2013).
- RabbitMQ documentation: "Consumer Acknowledgements and Publisher Confirms".
- Amazon SQS Developer Guide: visibility timeout and dead-letter queues.
