---
title: Event-driven architecture
section: backend
order: 3
phase: "Phase 1 · Backend architecture"
summary: Events vs commands, notification vs state transfer, choreography vs orchestration, the outbox, idempotent consumers and safe schema evolution.
---

In a request-driven system, the service that does something tells every other service what to do next: place the order, then call billing, then call email, then call search. In an event-driven system it announces what happened, `OrderPlaced`, and whoever cares reacts. That inversion removes two kinds of coupling. The consumer doesn't need to be up when the producer acts, and the producer doesn't need to know who the consumers are. It replaces them with a different set of problems: duplicates, ordering, schema evolution, and business flows that no single piece of code describes.

## Event-driven architecture

Components communicate by producing and consuming **events**: immutable records of something that happened, named in the past tense, published to a broker or log. A producer writes an event once. Zero, one or many consumers react, each at its own pace, and the producer never learns who they are.

### Events, commands and queries

All three travel as messages, but they mean different things, and mixing them up is the most common design smell.

| | Command | Event | Query |
|---|---|---|---|
| Intent | "Do this" | "This happened" | "Tell me" |
| Naming | Imperative: `ChargeCard` | Past tense: `PaymentCaptured` | `GetOrder` |
| Receivers | Exactly one owner | Zero to many | One |
| Can be rejected | Yes | No, it's a fact | n/a |
| Who depends on whom | Sender knows receiver | Consumer knows producer | Caller knows owner |

A command sent over a queue is still a command. That's fine, but name it as one. An event named `SendWelcomeEmail` is a command in disguise, and the producer is quietly coupled to the email service.

### How it works underneath

The broker is either a **log** or a **queue**:

| | Log (Kafka, Kinesis, Pulsar) | Queue (RabbitMQ, SQS) |
|---|---|---|
| After consumption | Message stays until retention expires | Message deleted on ack |
| Multiple consumers | Each group reads the whole log | Competing consumers share one queue (fan-out via exchanges or SNS) |
| Ordering | Per partition | Per queue with one consumer; SQS FIFO per message group |
| Replay | Yes, reset the offset | No |
| Position tracking | Consumer offset per partition | Per-message ack or visibility timeout |

```text
                  topic: orders.events
                +-----------------------+
 orders svc     | p0: e1 e4 e7          |--> billing (group A)
 key=order_id   | p1: e2 e5             |--> email   (group B)
 ------------>  | p2: e3 e6 e8 e9       |--> search  (group C)
                +-----------------------+
 same key -> same partition -> per-order ordering
 each consumer group tracks its own offsets and reads every event
```

See [Kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }}) and [RabbitMQ and SQS]({{ '/backend/rabbitmq-and-sqs/' | relative_url }}) for broker internals, and [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) for at-least-once versus exactly-once.

### Four patterns under one name

Martin Fowler's 2017 split is the clearest way to say which "event-driven" you mean:

1. **Event notification.** A thin event (`OrderPlaced {order_id}`). Consumers call back to the producer for details if they need them.
2. **Event-carried state transfer.** A fat event with the data consumers need (line items, totals, customer tier). Consumers keep a local copy and never call back.
3. **Event sourcing.** The event log is the source of truth, and current state is a fold over it ([Event sourcing and CQRS]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }})).
4. **CQRS.** Separate write and read models, with read models built from events.

| | Thin notification | Fat state transfer |
|---|---|---|
| Event size | Small | Larger |
| Consumer callbacks | Yes, load on producer | None |
| Producer availability needed | Yes, for the callback | No |
| Schema coupling | Low | Higher: consumers rely on fields |
| Staleness race | Callback may see newer state | Event is a consistent snapshot |

Rule of thumb: inside your own system, fat events with a deliberate, versioned schema usually win, because callbacks reintroduce the synchronous coupling you were trying to remove.

### Choreography vs orchestration

Multi-step business processes (order, then payment, then stock reservation, then shipment) can be coordinated two ways.

```text
 choreography: the flow lives in every subscriber
   orders --OrderPlaced--> payments --PaymentCaptured--> inventory
   inventory --StockReserved--> shipping

 orchestration: one coordinator owns state, timeouts, compensation
   +--------------+ --ChargeCard----> payments
   | order saga   | --ReserveStock--> inventory
   | orchestrator | --Ship----------> shipping
   +--------------+ <-- replies from each step
```

| | Choreography | Orchestration |
|---|---|---|
| Coordination | Each service reacts to events | A coordinator sends commands |
| Flow visibility | Implicit, spread across services | Explicit, one place |
| Coupling | Low, add consumers freely | Coordinator knows each step |
| Timeouts and compensation | Hard to reason about | Natural |
| Risk | "Pinball" flows nobody understands | The orchestrator becomes a god service |
| Good for | Fan-out side effects, few steps | Long processes with compensation |

Workflow engines such as Temporal or AWS Step Functions are orchestration with durable state built in. The saga mechanics are in [Distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}).

### Reliable publishing: dual writes and the outbox

The naive producer does two writes:

```python
# broken: two systems, no shared transaction
order.save()
producer.produce("orders.events", value=json.dumps(event))
```

If the process dies between the two lines, the order exists and nobody hears about it. Swap the order of the lines and you can publish an event for an order that then fails to commit. Django's `transaction.on_commit(lambda: publish(...))` narrows the window but doesn't close it: a crash right after commit still loses the event.

The **transactional outbox** fixes this. Write the event to an `outbox` table in the same database transaction as the business change. A separate relay reads unpublished rows, publishes them and marks them sent. Either both the order and its event commit or neither does. The relay can crash after publishing but before marking the row, so it republishes: that makes delivery at-least-once, and consumers must be idempotent. The relay is either a poller (simple) or CDC on the database log, such as Debezium's outbox event router on the Postgres WAL (lower latency, no polling load).

### Consuming safely

- **Expect duplicates.** At-least-once is the norm. Record each processed `event_id` in an inbox table in the same transaction as the side effect, or make the operation naturally idempotent (an upsert, setting a status).
- **Ordering is per partition, never global.** Key by the aggregate ID (`order_id`) so one entity's events stay in order. Keying by `tenant_id` gives you a hot partition for your biggest tenant.
- **Handle out-of-order and replayed events** with a version or sequence number per aggregate, and ignore anything older than what you have.
- **Commit offsets after processing**, not before, or a crash loses messages.
- **Poison messages** go to retry with backoff, then a dead-letter queue, so one bad event doesn't block its partition ([Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }})).
- **Watch consumer lag.** It's the queue depth of an event-driven system and the first signal that consumers can't keep up ([Backpressure]({{ '/backend/backpressure/' | relative_url }})).

### Event design and schema evolution

A useful envelope:

```json
{
  "event_id": "0b6c5c1e-7f0a-4c39-9a51-2f7f6f1d3c11",
  "type": "orders.OrderPlaced",
  "schema_version": 2,
  "occurred_at": "2026-11-16T09:30:12Z",
  "producer": "orders-service",
  "tenant_id": 4182,
  "aggregate_id": "ord_8f3k2",
  "aggregate_version": 3,
  "correlation_id": "req_71c9",
  "causation_id": "cmd_55ab",
  "payload": {
    "total_cents": 2500,
    "currency": "USD"
  }
}
```

`correlation_id` ties every event in one business flow together. `causation_id` points at the message that caused this one, which lets you rebuild the chain when debugging.

Evolution rules:

- **Additive changes only**: add optional fields with defaults. Never rename, remove or change a field's type in place.
- **Breaking change means a new event type or topic** (`orders.v2`). Run both until consumers migrate.
- **Tolerant readers**: consumers ignore unknown fields and don't fail on missing optional ones.
- **Schema registry** for Avro, Protobuf or JSON Schema. Confluent Schema Registry checks compatibility on registration: `BACKWARD` (the default) means consumers on the new schema can read data written with the previous one, so you upgrade consumers first. `FORWARD` means old consumers can read new data, so you upgrade producers first. `FULL` is both.
- **Separate internal from public events.** Domain events inside a service can change freely. Integration events published to other teams are a contract, so don't serialize your ORM model into them.

### When to use it

- One fact, many reactions: an order triggers billing, email, analytics, search indexing and fraud checks.
- Slow or unreliable side effects that shouldn't block the user's request.
- Integration across bounded contexts or teams without synchronous dependencies.
- Absorbing traffic spikes: the log buffers while consumers catch up.
- Audit trails, replay and rebuilding read models.

### When not to

- The user needs the result now and consistently, such as checking whether a payment succeeded before showing a confirmation. Use a synchronous call, or a command plus a status endpoint.
- Simple CRUD with one consumer. A function call or a job queue is simpler.
- You need strict global ordering across all entities.
- Request/reply disguised as events: a `QuoteRequested` event plus a `QuoteCalculated` event is a slower, harder-to-debug RPC.
- No observability yet. Without tracing and an event catalog, nobody can explain why an email went out.

Real-world example: LinkedIn built Kafka to move activity and change data between systems as a log, publicly described and open-sourced in 2011, and most large consumer companies now run event pipelines on Kafka or a managed equivalent.

## In practice

### Runnable: outbox relay and idempotent consumer

Two SQLite databases stand in for two services. The relay crashes after publishing but before marking the row, so the event goes out twice, and the consumer's inbox table keeps the effect exactly once.

```python
import json
import sqlite3
import uuid


def setup_orders(db):
    db.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, total_cents INTEGER NOT NULL);
        CREATE TABLE outbox (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            payload TEXT NOT NULL,
            published_at TEXT
        );
    """)


def setup_billing(db):
    db.executescript("""
        CREATE TABLE inbox (event_id TEXT PRIMARY KEY);
        CREATE TABLE revenue (id INTEGER PRIMARY KEY CHECK (id = 1), cents INTEGER);
        INSERT INTO revenue VALUES (1, 0);
    """)


def place_order(db, total_cents):
    with db:  # one transaction: business row and event row
        cur = db.execute("INSERT INTO orders (total_cents) VALUES (?)", (total_cents,))
        event = {
            "event_id": str(uuid.uuid4()),
            "type": "OrderPlaced",
            "order_id": cur.lastrowid,
            "total_cents": total_cents,
        }
        db.execute(
            "INSERT INTO outbox (id, topic, payload) VALUES (?, ?, ?)",
            (event["event_id"], "orders.events", json.dumps(event)),
        )
    return cur.lastrowid


def relay(db, broker, crash_after_publish=False):
    rows = db.execute(
        "SELECT id, topic, payload FROM outbox"
        " WHERE published_at IS NULL ORDER BY rowid"
    ).fetchall()
    for event_id, topic, payload in rows:
        broker.append((topic, payload))
        if crash_after_publish:
            raise ConnectionError("relay died before marking the row")
        with db:
            db.execute(
                "UPDATE outbox SET published_at = datetime('now') WHERE id = ?",
                (event_id,),
            )


def consume(db, message):
    event = json.loads(message[1])
    with db:  # dedupe and side effect commit together
        try:
            db.execute("INSERT INTO inbox (event_id) VALUES (?)", (event["event_id"],))
        except sqlite3.IntegrityError:
            return False  # already applied
        db.execute(
            "UPDATE revenue SET cents = cents + ? WHERE id = 1",
            (event["total_cents"],),
        )
    return True


if __name__ == "__main__":
    orders_db, billing_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    setup_orders(orders_db)
    setup_billing(billing_db)
    broker = []

    place_order(orders_db, 2500)
    place_order(orders_db, 1000)
    try:
        relay(orders_db, broker, crash_after_publish=True)
    except ConnectionError:
        pass
    relay(orders_db, broker)  # restart: first event goes out again

    assert len(broker) == 3
    applied = [consume(billing_db, m) for m in broker]
    assert applied == [True, False, True]
    (cents,) = billing_db.execute("SELECT cents FROM revenue").fetchone()
    assert cents == 3500
    pending = orders_db.execute(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL"
    ).fetchone()[0]
    assert pending == 0
    print("published", len(broker), "applied", sum(applied), "revenue", cents)
```

### The same outbox in Django

Illustrative. The outbox row is written inside the same `transaction.atomic()` block as the order:

```python
# orders/models.py and services.py (illustrative)
import uuid

from django.db import models, transaction


class OutboxEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    topic = models.CharField(max_length=100)
    key = models.CharField(max_length=100)  # partition key, e.g. order id
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, db_index=True)


def place_order(cmd):
    with transaction.atomic():
        order = Order.objects.create(tenant_id=cmd.tenant_id, total_cents=cmd.total_cents)
        OutboxEvent.objects.create(
            topic="orders.events",
            key=str(order.id),
            payload=order_placed_payload(order),
        )
    return order
```

A relay process claims a batch with `SKIP LOCKED` so several relays never publish the same row, publishes, and marks the batch. Illustrative, using `confluent_kafka`:

```python
import json

from django.db import transaction
from django.utils import timezone


def relay_batch(producer, batch_size=500):
    with transaction.atomic():
        batch = list(
            OutboxEvent.objects.filter(published_at__isnull=True)
            .order_by("created_at")
            .select_for_update(skip_locked=True)[:batch_size]
        )
        for e in batch:
            producer.produce(e.topic, key=e.key, value=json.dumps(e.payload))
        producer.flush(10)  # returns messages still queued; check delivery callbacks
        OutboxEvent.objects.filter(id__in=[e.id for e in batch]).update(
            published_at=timezone.now()
        )
    return len(batch)
```

Two caveats. `flush()` doesn't raise on delivery failure, so register an `on_delivery` callback and roll back the batch if any message failed. And with several relays, events for one key can be published out of order. Run a single relay, or shard relays by key.

### A Kafka consumer that commits after processing

Illustrative:

```python
import json

from confluent_kafka import Consumer

consumer = Consumer({
    "bootstrap.servers": "kafka:9092",
    "group.id": "billing",
    "enable.auto.commit": False,
    "auto.offset.reset": "earliest",
})
consumer.subscribe(["orders.events"])

while True:
    msg = consumer.poll(1.0)
    if msg is None:
        continue
    if msg.error():
        log.warning("kafka error: %s", msg.error())
        continue
    handle(json.loads(msg.value()))  # idempotent via inbox table
    consumer.commit(message=msg, asynchronous=False)
```

### Ignoring stale events with a version check

With event-carried state transfer, an upsert guarded by the aggregate version makes the consumer safe against both duplicates and reordering:

```sql
INSERT INTO product_copy (product_id, name, price_cents, version)
VALUES (%(id)s, %(name)s, %(price_cents)s, %(version)s)
ON CONFLICT (product_id) DO UPDATE
SET name = EXCLUDED.name,
    price_cents = EXCLUDED.price_cents,
    version = EXCLUDED.version
WHERE product_copy.version < EXCLUDED.version;
```

## Pitfalls

- **Dual writes.** Saving and publishing without an outbox loses events on crashes. Symptom: downstream counts drift from the source and nobody can say why.
- **Non-idempotent consumers.** Duplicate emails, double credits. Every handler needs an inbox table or an idempotent write.
- **Wrong partition key.** Keying by tenant concentrates the largest tenant on one partition, while keying randomly loses per-entity ordering. Key by aggregate ID.
- **Head-of-line blocking.** One failing message retried in place stalls its whole partition. Use bounded retries, retry topics and a DLQ.
- **Breaking schema changes.** A renamed field crashes consumers at 3 a.m. Enforce compatibility in a schema registry or in CI.
- **Callback storms.** Thin events make every consumer call the producer at once on every change. Carry the needed state in the event.
- **Invisible flows.** Choreography across eight services with no trace context. Propagate `traceparent` and correlation IDs in message headers and keep an event catalog that lists producers and consumers.
- **Eventual consistency in the UI.** Read from the owner right after a write, or show "processing" instead of stale data.

## Interview questions

**What's the difference between an event and a command?**
A command asks one specific owner to do something and can be rejected. An event states a fact that already happened, can't be rejected, and can have any number of consumers. Commands couple the sender to the receiver, while events couple the consumer to the producer's schema.

**How do you publish an event if and only if the database transaction commits?**
Transactional outbox: write the event row in the same transaction, then relay it with a poller or CDC. Delivery becomes at-least-once, so consumers deduplicate.

**How do you handle duplicate messages?**
Make consumers idempotent. Store processed event IDs in the same transaction as the effect, use natural idempotency (upserts, state transitions guarded by the current state), and carry idempotency keys to external APIs.

**How do you preserve ordering?**
Only per key. Partition by aggregate ID, keep one in-flight message per partition for order-sensitive handlers, and use per-aggregate version numbers so consumers can ignore stale or replayed events.

**Choreography or orchestration?**
Choreography for simple fan-out where producers shouldn't know consumers. Orchestration for multi-step processes that need timeouts, compensations and a visible state machine.

**How do you evolve event schemas without breaking consumers?**
Additive changes, tolerant readers, a schema registry with compatibility checks, and a new event type or topic for breaking changes, with both versions running until consumers move.

**When is event-driven architecture the wrong choice?**
When the caller needs an immediate, consistent answer, when there's one consumer and a function call would do, when you need global ordering, or when the team lacks tracing and ownership of event contracts.

## Further reading

- Martin Fowler, "What do you mean by 'Event-Driven'?" (martinfowler.com, 2017).
- Gregor Hohpe and Bobby Woolf, *Enterprise Integration Patterns* (Addison-Wesley, 2003).
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 2017), chapter 11 on stream processing.
- Chris Richardson, *Microservices Patterns* (Manning, 2018), on the transactional outbox and sagas.
- Debezium documentation, "Outbox Event Router".
