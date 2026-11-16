---
title: Message ordering and dead letter queues
section: backend
order: 46
phase: "Phase 6 · Messaging and events"
summary: Get per-key ordering from partitions and FIFO groups, repair out-of-order events with sequence numbers, and run DLQs with redrive and alerting.
---

Ordering and poison messages pull against each other. The strictest ordering means one message that can never succeed blocks everything queued behind it; a dead letter queue unblocks the line but takes that message out of order. Design the two together: decide what order you actually need (almost always per entity, not global), and decide what happens to that entity's later messages when one of its messages is dead-lettered.

## Message ordering

### Why global ordering is expensive

A total order across all messages needs a single sequencer and, to preserve it through processing, a single consumer. Throughput is then capped at what one consumer can do. Business rules rarely need that. They need per-entity order: one account's debits in order, one order's status transitions in order, one chat's messages in order. Different entities can be processed in parallel.

Order gets lost in more places than people expect:

- Multiple producers: no shared clock, so "before" across producers is not defined.
- Producer retries with several requests in flight: batch 2 succeeds while batch 1 is retried.
- Multiple partitions or queues: no order between them.
- Concurrent consumers or a thread pool inside one consumer.
- Redelivery: a nacked or timed-out message comes back after later ones were processed.
- Retry via delay queues or republishing to the same topic: the retry lands at the tail.
- Rebalances and partition-count changes: keys move to new partitions mid-stream.
- DLQ redrive: old messages are replayed after newer ones.

### Per-partition and per-key ordering

Kafka guarantees order within a partition only. The producer hashes the key (murmur2 in the default partitioner) to pick a partition, so all events with the same key land in the same partition in send order, and a consumer group assigns each partition to one consumer at a time.

```text
 key=acct-7 --+                part 0: [a7-1][a7-2][a7-3] -> consumer A
 key=acct-9 --+-> hash(key) -> part 1: [a9-1][a9-2]       -> consumer B
 key=acct-4 --+   % 3          part 2: [a4-1][a4-2][a4-3] -> consumer C

 order holds per key; no order between acct-7 and acct-9 (not needed)
```

What keeps it intact:

- **Idempotent producer.** With `enable.idempotence=true` (the default since Kafka 3.0), ordering is preserved with up to five in-flight requests per connection. Without idempotence, retries with more than one in-flight request can reorder.
- **Fixed partition count.** Adding partitions changes `hash(key) % n`, so a key's new events go to a different partition while its old ones are still being consumed elsewhere. Overprovision partitions up front, or migrate to a new topic.
- **Serial processing per key.** To parallelize within a partition, shard records to N worker threads by `hash(key) % N` and commit only up to the lowest offset all workers have finished. Libraries such as Confluent's parallel consumer implement this pattern.
- **Hot keys** cap throughput at one partition. If one tenant produces 40% of traffic, per-tenant keys are wrong; key by a finer entity (account, order) whose order actually matters.

### FIFO groups

| System | Ordering unit | How you get it | Cost |
|---|---|---|---|
| Kafka | Partition | Message key | Hot keys, fixed partition count |
| SQS FIFO | Message group | `MessageGroupId` | Per-group serial; lower throughput than standard queues |
| RabbitMQ | Queue | Single consumer or single active consumer; consistent-hash exchange to shard by key | Throughput per queue |
| Google Pub/Sub | Ordering key | `ordering_key` with ordering enabled on the subscription | Per-key serial |
| Azure Service Bus | Session | `SessionId` | Session lock per consumer |

SQS FIFO delivers messages of one group in order and does not hand out later messages of that group while earlier ones are in flight. Many groups give parallelism; one group is fully serial. FIFO throughput limits are lower than standard queues and have changed over time (high-throughput mode raised them substantially), so check current quotas rather than quoting a number.

RabbitMQ keeps FIFO within a queue, but with several consumers and a prefetch window, processing order differs from queue order, and a requeued message goes back near the head while later ones are already done. The single active consumer queue argument (`x-single-active-consumer`) keeps one consumer active with automatic failover; combine it with a consistent-hash exchange over N queues to get per-key order with N-way parallelism.

### Sequence numbers

When the transport cannot be trusted to keep order, put the order in the message. The producer stamps a per-entity, gapless sequence: the aggregate version from [event sourcing]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }}) or a row version incremented in the same transaction. The consumer stores the last applied sequence per entity:

- `seq <= last`: duplicate or stale. Drop and ack.
- `seq == last + 1`: apply, then apply any buffered successors.
- `seq > last + 1`: a gap. Buffer for a bounded time, then fetch current state from the source of truth, or park the entity.

Timestamps are a weak substitute: clocks skew between hosts and two events can share a millisecond. Use them only where last-write-wins is acceptable.

### Out-of-order handling

| Strategy | How | Works for |
|---|---|---|
| Make order irrelevant | Commutative operations, sets, idempotent absolute state | Counters, tags, "status is X" |
| Version-guarded write | `UPDATE ... WHERE version < :v`; stale events become no-ops | State-transfer events |
| Resequencer | Buffer per key until the gap fills; bounded by time and size | Delta events that must all apply |
| Fetch on gap | Treat the event as a notification; read current state from the owner's API | When the owner can serve reads |
| State machine guard | Reject transitions invalid from the current state; retry later | Workflows (`Shipped` before `Paid`) |
| Watermarks | Event-time windows with allowed lateness | Stream analytics (Flink, Beam) |

Version-guarded writes are the most common production answer because they need no buffer: send the entity's full state with its version, and older versions lose.

```sql
UPDATE orders
   SET status = 'shipped', shipped_at = :shipped_at, version = :v
 WHERE id = :order_id AND version < :v;   -- 0 rows => stale or duplicate: ack
```

## Dead letter queues

### Poison messages

A poison message can never succeed no matter how many times it is retried: malformed JSON, an unknown schema version, a reference to an entity that does not exist, a payload that trips a bug, a value that violates a business rule. Transient failures are different: timeouts, 503s, deadlocks, rate limits. These succeed on retry.

Without a DLQ, a poison message is redelivered forever. On a queue it burns consumer capacity and floods logs; on a Kafka partition it blocks every message behind it (head-of-line blocking), and lag on that partition grows without bound. A DLQ is a holding area: move the message aside, keep the line moving, and let a human or a fix deal with it later.

### Max receives and retry policy

Classify the error first:

- **Permanent** (validation, deserialization, unknown type): dead-letter immediately. Retrying wastes time.
- **Transient** (network, 5xx, lock timeout): retry with exponential backoff and jitter (see [Retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})), then dead-letter after N attempts.
- **Unknown exceptions**: treat as transient with a small N, so bugs surface in the DLQ quickly.

How each broker counts attempts:

- **SQS**: a redrive policy on the source queue with `maxReceiveCount`. Every receive increments the count, including receives where the consumer crashed or the visibility timeout expired, so a value of 1 dead-letters on any hiccup. 3 to 10 is typical. The DLQ must be the same type (FIFO for FIFO) and, for standard queues, retention counts from the original enqueue time, so give the DLQ the maximum retention (14 days).
- **RabbitMQ**: a dead-letter exchange (`dead-letter-exchange` policy or `x-dead-letter-exchange` argument). Messages are dead-lettered when rejected with `requeue=false`, when their TTL expires, when the queue length limit is hit, or when a quorum queue's delivery limit is exceeded (RabbitMQ 4.x sets a default limit of 20). The `x-death` header records the history.
- **Kafka**: no broker-side DLQ. The consumer publishes the failed record to a `<topic>.dlq` topic and commits the offset. Kafka Connect has `errors.deadletterqueue.topic.name`; Spring Kafka has a dead-letter publishing recoverer. Uber publicly described a tiered pattern: `orders` to `orders.retry.1m` to `orders.retry.10m` to `orders.dlq`, each retry topic consumed after a delay, so the main topic never blocks.

```bash
# SQS: dead-letter after 5 receives (illustrative)
cat > redrive.json <<'EOF'
{"RedrivePolicy": "{\"deadLetterTargetArn\":\"arn:aws:sqs:us-east-1:123456789012:orders-dlq\",\"maxReceiveCount\":\"5\"}"}
EOF
aws sqs set-queue-attributes --queue-url "$ORDERS_URL" --attributes file://redrive.json

# RabbitMQ: DLX plus delivery limit for a quorum queue (illustrative)
rabbitmqctl set_policy orders-dlx '^orders$' \
  '{"dead-letter-exchange":"orders.dlx","delivery-limit":5}' --apply-to queues
```

### DLQs versus ordering

Dead-lettering message 3 of `order-9` and processing message 4 breaks per-key order. Choose deliberately:

| Option | Behavior | Use when |
|---|---|---|
| Accept it | Later messages proceed; version guards drop stale replays | State-transfer events, independent commands |
| Park the key | Record `order-9` as blocked; route its later messages to the DLQ too until resolved | Delta events per entity (ledgers) |
| Block the partition | Stop consuming until fixed | Strict global ledgers; rare, pages someone |

SQS FIFO moves a message to the DLQ after `maxReceiveCount` and then continues with the rest of the group; AWS's own guidance warns that a DLQ on a FIFO queue can break the exact order of operations. If order matters, parking is on you.

### Inspection and redrive

A DLQ entry is only useful if someone can act on it. Store:

- the original payload bytes and headers, unmodified;
- source queue or topic, partition and offset;
- error class, message and stack trace, from the last attempt;
- attempt count, first and last failure time;
- the consumer version (build sha) that failed.

Then provide tooling: list and group by error type, peek at a message, replay one or all after a fix, discard with a recorded reason. SQS supports redriving a DLQ back to its source queue from the console or the `StartMessageMoveTask` API; RabbitMQ can move messages back with a shovel; Kafka needs a small replay consumer that reads the DLQ topic and produces to the main or a retry topic.

Redrive safely: consumers must be idempotent (some messages may have partly succeeded), replayed state events need version guards so they do not overwrite newer state, and redrive should be rate-limited so a 500,000-message replay does not take down the dependency that caused the failures.

### Alerting

A DLQ nobody watches is a slow data-loss mechanism: messages expire at the end of retention.

- Alert on DLQ inflow rate and on depth above zero for low-volume, high-value queues.
- Alert on age of the oldest DLQ message (`ApproximateAgeOfOldestMessage` on SQS) well before it reaches retention.
- Track the DLQ-to-processed ratio; a jump after a deploy points at the deploy.
- Route alerts to the team that owns the consumer, with a runbook covering inspect, fix, redrive.

## In practice

### Runnable: resequencer and DLQ with max receives

```python
from collections import defaultdict, deque


class Resequencer:
    """Apply each key's events in sequence order; drop duplicates, buffer gaps."""

    def __init__(self, apply, max_gap_buffer=100):
        self.apply = apply
        self.last = defaultdict(int)     # key -> last applied sequence number
        self.pending = defaultdict(dict)  # key -> {seq: event} waiting on a gap
        self.max_gap_buffer = max_gap_buffer

    def receive(self, key, seq, ev):
        if seq <= self.last[key]:
            return 0                      # duplicate or stale redelivery
        self.pending[key][seq] = ev
        if len(self.pending[key]) > self.max_gap_buffer:
            raise OverflowError(f"{key}: gap after {self.last[key]}, refetch state")
        applied = 0
        while self.last[key] + 1 in self.pending[key]:
            self.last[key] += 1
            self.apply(key, self.pending[key].pop(self.last[key]))
            applied += 1
        return applied


class PermanentError(Exception):
    """Retrying cannot help: bad payload, unknown schema, business rule."""


def consume(queue, handler, max_receives=3):
    """At-least-once loop with a receive counter and a dead letter queue."""
    receives, dlq = defaultdict(int), []
    while queue:
        msg = queue.popleft()
        receives[msg["id"]] += 1
        try:
            handler(msg)
        except Exception as exc:
            permanent = isinstance(exc, PermanentError)
            if permanent or receives[msg["id"]] >= max_receives:
                dlq.append({"message": msg, "error": repr(exc),
                            "receives": receives[msg["id"]]})
            else:
                queue.append(msg)         # real systems: redeliver after backoff
    return dlq


if __name__ == "__main__":
    # Out-of-order and duplicated deliveries for one order's status events.
    log = []
    rs = Resequencer(lambda key, ev: log.append(ev))
    arrivals = [(1, "created"), (3, "shipped"), (2, "paid"), (2, "paid"), (4, "delivered")]
    applied = [rs.receive("order-9", seq, ev) for seq, ev in arrivals]
    assert log == ["created", "paid", "shipped", "delivered"]
    assert applied == [1, 0, 2, 0, 1]

    # Poison and transient failures.
    attempts = defaultdict(int)

    def handler(msg):
        attempts[msg["id"]] += 1
        if msg["id"] == "flaky" and attempts["flaky"] < 3:
            raise TimeoutError("upstream timeout")         # succeeds on 3rd try
        if msg["id"] == "down":
            raise ConnectionError("payments API down")     # never recovers
        if "amount" not in msg:
            raise PermanentError("missing field: amount")  # poison

    q = deque([{"id": "ok", "amount": 1}, {"id": "flaky", "amount": 2},
               {"id": "poison"}, {"id": "down", "amount": 3}])
    dlq = consume(q, handler, max_receives=3)
    by_id = {d["message"]["id"]: d for d in dlq}
    assert set(by_id) == {"poison", "down"}
    assert by_id["poison"]["receives"] == 1   # permanent: no pointless retries
    assert by_id["down"]["receives"] == 3     # transient: retried, then parked
    assert attempts["flaky"] == 3

    # Redrive after the fix ships (here: the API recovers, payload repaired).
    def fixed_handler(msg):
        if "amount" not in msg:
            raise PermanentError("missing field: amount")
    repaired = [dict(d["message"], amount=0) if d["message"]["id"] == "poison"
                else d["message"] for d in dlq]
    assert consume(deque(repaired), fixed_handler) == []
    print("ok", [d["error"] for d in dlq])
```

### Kafka consumer with a DLQ topic (illustrative)

```python
def handle(msg):
    try:
        process(json.loads(msg.value()))
    except (ValueError, KeyError) as exc:                 # permanent: bad payload
        producer.produce(
            "orders.dlq", key=msg.key(), value=msg.value(),
            headers=[("error", repr(exc).encode()),
                     ("source", f"{msg.topic()}/{msg.partition()}/{msg.offset()}".encode()),
                     ("consumer_version", BUILD_SHA.encode())])
        producer.flush(10)
    # transient errors raise out of handle(); the loop retries with backoff
    consumer.commit(message=msg, asynchronous=False)      # unblock the partition
```

Keep messages for one key on one partition with `producer.produce("orders", key=str(order_id), value=payload)`; never key by a random id if order matters.

## Pitfalls

- **Assuming topic-level order.** Kafka orders within a partition; SQS standard does not order at all.
- **Adding partitions to a keyed topic.** Keys remap, and old and new events for one key are consumed in parallel. Plan partition counts; migrate topics instead.
- **A thread pool behind a single consumer.** Fast messages overtake slow ones. Shard by key.
- **Retry by republishing to the same topic.** The retry lands behind newer events. Use version guards or tiered retry topics.
- **Dead-lettering an outage.** A downstream API is down for an hour, every message exhausts its retries, and the DLQ fills with a million healthy messages. Pause consumption with a circuit breaker (see [Fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }})) when the error is clearly systemic.
- **`maxReceiveCount` of 1 or 2.** Any crash or slow deploy dead-letters good messages.
- **DLQ without context.** Payload only, no error or source; nobody can tell what failed or why.
- **Unmonitored DLQ.** Messages age out silently after retention.
- **Unthrottled redrive.** Replaying everything at once recreates the original incident.

## Interview questions

**How do you guarantee per-user ordering in Kafka at scale?** Key messages by user id so each user's events go to one partition; use the idempotent producer; process each partition serially or shard by key inside the consumer; keep the partition count fixed. Parallelism comes from many partitions, not from parallelism within a key.

**What happens to ordering when you add partitions?** Keys hash to different partitions, so new events for a key can be processed before its older events still sitting in the old partition. Overprovision partitions, or create a new topic and cut over once the old one drains.

**A consumer receives version 5 of an order before version 4. What do you do?** If events carry full state, apply 5 and drop 4 when it arrives (version guard). If they are deltas, buffer 5 until 4 arrives with a timeout, then fetch current state from the owner or park the order.

**How do you choose `maxReceiveCount`?** High enough that crashes, deploys and short transient failures do not dead-letter good messages (3 to 10), low enough that poison messages surface quickly. Pair it with a visibility timeout above worst-case processing time, and dead-letter permanent errors immediately in code.

**A downstream outage dumped a million messages into the DLQ. How do you prevent and recover?** Prevent with a circuit breaker that pauses consumption when the failure rate is systemic. Recover with an idempotent, rate-limited redrive after the dependency is healthy, watching its error rate.

**How do DLQs interact with FIFO ordering?** Moving one message aside lets later messages of the same key proceed out of order. Either make events order-insensitive with version guards, or park the whole key until the dead-lettered message is resolved.

**Kafka has no DLQ. How do you build one?** Catch failures in the consumer, publish the original record plus error headers to a DLQ topic, then commit the offset. For transient errors, add delayed retry topics so retries do not block the main partition.

## Further reading

- Gregor Hohpe and Bobby Woolf, *Enterprise Integration Patterns*: Resequencer, Dead Letter Channel, Invalid Message Channel
- Amazon SQS Developer Guide: FIFO queues, dead-letter queues and redrive
- RabbitMQ documentation: Dead Letter Exchanges; Quorum Queues (poison message handling); Consumers (single active consumer)
- Apache Kafka documentation: producer configs `enable.idempotence` and `max.in.flight.requests.per.connection`
- Uber Engineering, "Building Reliable Reprocessing and Dead Letter Queues with Apache Kafka" (2018)
