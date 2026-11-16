---
title: Delivery semantics
section: backend
order: 45
phase: "Phase 6 · Messaging and events"
summary: At-most-once, at-least-once and exactly-once effects. Where duplicates and losses come from, and how inboxes, outboxes and Kafka transactions fix them.
---

A message crosses three hand-offs: producer to broker, broker to consumer, consumer to its side effect (a database row, an email, a charge). Each hand-off can fail *after* the work happened but *before* the confirmation arrived. The sender then has two choices: assume it worked and move on (risk loss) or try again (risk a duplicate). Delivery semantics is just the name for which risk you chose, and exactly-once is what you get when you choose duplicates and then make them harmless.

```text
 producer          broker              consumer            database
    | -- publish --> |                    |                    |
    | <-- ack lost   |                    |                    |
    | -- retry ----> |  stored twice (dup #1)                  |
    |                | -- deliver ------> | -- UPDATE -------> |
    |                |                    | crash before ack   |
    |                | -- redeliver ----> | -- UPDATE again -> |  dup #2
```

| Failure point | At-most-once outcome | At-least-once outcome |
|---|---|---|
| Publish ack lost | Message may be missing | Producer retries: duplicate in broker |
| Consumer crashes mid-processing | Message lost | Redelivered: may half-apply twice |
| Consumer crashes after effect, before ack | Fine | Redelivered: effect applied twice |
| Processing exceeds visibility timeout | Fine | Second consumer gets it concurrently |

## At-most-once delivery

The receiver acknowledges before doing the work, and the sender never retries. A crash between ack and effect loses the message; nothing is ever processed twice.

How you get it:

- Kafka consumer: commit the offset *before* processing (`commitSync`, then handle). Producer with `acks=0` or retries disabled.
- RabbitMQ: `auto_ack=True`; the broker considers the message delivered as soon as it is written to the socket.
- Celery's default: a task is acknowledged just before it executes, so a worker that dies mid-task loses it.
- UDP fire-and-forget, StatsD-style metrics.

Use it when the next message supersedes the last and a gap is invisible: telemetry samples, presence heartbeats, live location pings, cache-warming hints, debug logs. It is the cheapest option: no dedup state, no redelivery storms. Never use it for anything that moves money, changes ownership or notifies a human who will act on it.

## At-least-once delivery

The sender retries until it gets an ack, and the receiver acks only after the effect is durable. Nothing is lost, but anything can arrive more than once. This is the default for nearly every serious queue:

- Kafka: producer `acks=all` with retries; consumer commits offsets after processing. Rebalances and crashes replay everything since the last commit.
- RabbitMQ: publisher confirms on the producer, manual acks on the consumer. Unacked messages are requeued when the channel closes.
- SQS: a received message is hidden for the visibility timeout; if it is not deleted in time, it reappears. Standard queues may also deliver a copy more than once even without failures.
- Celery with `task_acks_late=True`: ack after the task returns. With the Redis broker, a task not acked within `visibility_timeout` (one hour by default) is redelivered, so long-running and far-future ETA tasks run twice.

Duplicates are therefore normal input, not an edge case. Their sources: producer retries after a lost ack, consumer crashes before the ack, visibility timeouts shorter than processing time, rebalances, DLQ redrives, and operators replaying a topic. At-least-once is only half a design; the other half is the idempotent consumer below.

## Exactly-once processing

### Exactly-once delivery is impossible; exactly-once effects are not

Over a network that can drop the ack, the sender cannot distinguish "never arrived" from "arrived, ack lost" (the Two Generals problem). So no protocol delivers a message exactly once to an arbitrary receiver. What systems advertise as exactly-once is an exactly-once *effect*:

```text
 exactly-once effect = at-least-once delivery
                     + idempotent processing
                     + atomically recording what was processed
```

Kafka's exactly-once semantics, SQS FIFO deduplication and Flink checkpoints are all instances of this formula with a narrow scope. The question to ask is always: exactly-once *between which two points*?

### Idempotent consumers

Make the effect safe to repeat. In order of preference:

- **Naturally idempotent operations.** `SET status = 'shipped'`, upsert by business key, `DELETE ... WHERE id = :id`. Repeating them is harmless.
- **Version-guarded updates.** `UPDATE orders SET status = :s, version = :v WHERE id = :id AND version < :v` ignores replays and stale events.
- **Absolute values over deltas.** Send "balance is 70" or "item count is 3" rather than "subtract 50", when the producer knows the result.
- **Idempotency keys downstream.** When the effect is an external API call, pass a key derived from the message id; payment APIs such as Stripe deduplicate requests carrying the same `Idempotency-Key`. See [Idempotency]({{ '/backend/idempotency/' | relative_url }}).

### Dedup tables and the transactional inbox

For non-idempotent effects (increment a balance, append a ledger line), record the message id in the *same database transaction* as the effect:

```sql
BEGIN;
INSERT INTO inbox (consumer, message_id)
VALUES ('billing', '7f3c9e1a-...') ON CONFLICT DO NOTHING;
-- 0 rows inserted => already processed: COMMIT, then ack
UPDATE wallets SET balance_cents = balance_cents + 1000 WHERE id = 42;
COMMIT;
-- ack the broker only now; a crash before the ack is a harmless redelivery
```

Details that decide whether this works:

- **The id must be stable across retries.** Use an event id the producer generated when the fact happened. Broker ids are not enough: an SQS `MessageId` differs on a producer resend, and a Kafka `(partition, offset)` catches redelivery but not a producer duplicate.
- **Same transaction, no exceptions.** If the inbox insert commits separately and the process dies before the effect, the redelivery is skipped and the message is lost. The demo below shows this exact failure.
- **Retention covers the replay horizon.** Keep ids at least as long as messages can come back: SQS retention (up to 14 days), DLQ redrives, topic replays. Partition the inbox by day and drop old partitions.
- **Inbox as a buffer.** A variant stores the raw message in the inbox and acks immediately, then a local worker processes inbox rows. That decouples broker acks from slow processing and gives you a durable local queue (see [Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }})).
- A Redis `SET key NX EX 86400` dedup is not atomic with your database write. It is fine as a best-effort filter in front of an effect that is already idempotent.

### Transactional outbox

The producer side has the mirror problem, the dual write: commit an order, then publish `OrderPlaced`. Crash between the two and the event is lost; publish first and the order may roll back, leaving a phantom event. The outbox writes the event into a table in the same transaction as the business change, and a relay publishes it afterwards:

```text
 app txn: INSERT orders + INSERT outbox ---> COMMIT
                                               |
 relay (poll with SKIP LOCKED, or CDC via Debezium reading the WAL)
                                               |
                                    publish ---+---> mark sent
                                 (at-least-once: crash => republish)
```

Illustrative Django code (`confluent_kafka` producer API):

```python
import json
import uuid

from django.db import transaction
from django.utils.timezone import now


def place_order(cart, user):
    with transaction.atomic():
        order = Order.objects.create(user=user, total=cart.total)
        OutboxEvent.objects.create(
            event_id=uuid.uuid4(),                 # stable id for consumer dedup
            aggregate_id=str(order.id),
            event_type="OrderPlaced",
            payload={"order_id": str(order.id), "total": str(order.total)},
        )
    return order


def relay_batch(producer, batch_size=200):
    with transaction.atomic():
        events = list(OutboxEvent.objects.select_for_update(skip_locked=True)
                      .filter(sent_at__isnull=True).order_by("id")[:batch_size])
        failed = []
        for e in events:
            producer.produce(
                "orders", key=e.aggregate_id, value=json.dumps(e.payload),
                headers={"event_id": str(e.event_id), "type": e.event_type},
                on_delivery=lambda err, msg: err and failed.append(err),
            )
        if producer.flush(10) or failed:           # undelivered => roll back, retry
            raise RuntimeError("outbox publish incomplete")
        OutboxEvent.objects.filter(id__in=[e.id for e in events]).update(sent_at=now())
```

The relay is at-least-once (crash after publish, before `sent_at`), so consumers still need the inbox. Ordering per aggregate holds if the relay publishes in id order with the aggregate id as the message key. CDC relays avoid polling load and preserve commit order but add Kafka Connect to your stack.

### Kafka idempotent producer and transactions

- **Idempotent producer** (`enable.idempotence=true`, the default since Kafka 3.0 along with `acks=all`). The broker tracks a producer id and per-partition sequence numbers and drops retried batches it has already written. Scope: one producer session, per partition. It does not catch your application calling `send()` twice or a restarted process resending.
- **Transactions** (`transactional.id`). A producer writes to several partitions *and* commits the consumer's offsets as one atomic unit with `sendOffsetsToTransaction`. Consumers with `isolation.level=read_committed` never see aborted writes. The transactional id carries an epoch that fences zombie instances after a failover.
- **Kafka Streams** wraps this as `processing.guarantee=exactly_once_v2`.

The scope is Kafka in, Kafka out. A consume-transform-produce pipeline gets exactly-once effects across topics; a consumer that writes to Postgres or calls an HTTP API gets nothing from Kafka transactions and still needs an inbox or idempotency key. Costs: extra latency per commit, and `read_committed` consumers cannot read past the last stable offset, so one long transaction stalls them.

Illustrative read-process-write loop (`confluent_kafka`):

```python
consumer = Consumer({"bootstrap.servers": "kafka:9092", "group.id": "enricher",
                     "enable.auto.commit": False, "isolation.level": "read_committed"})
producer = Producer({"bootstrap.servers": "kafka:9092",
                     "transactional.id": "enricher-0"})   # stable per instance
producer.init_transactions()
consumer.subscribe(["orders"])

while True:
    batch = [m for m in consumer.consume(num_messages=500, timeout=1.0) if not m.error()]
    if not batch:
        continue
    producer.begin_transaction()
    try:
        for m in batch:
            producer.produce("orders-enriched", key=m.key(), value=enrich(m.value()))
        producer.send_offsets_to_transaction(
            consumer.position(consumer.assignment()),
            consumer.consumer_group_metadata())
        producer.commit_transaction()
    except Exception:
        producer.abort_transaction()
        raise   # restart: consumption resumes from the last committed offsets
```

| Guarantee | Ack timing | On failure | Extra state | Use for |
|---|---|---|---|---|
| At-most-once | Before processing | Loss | None | Telemetry, presence, hints |
| At-least-once | After processing | Duplicates | None | Default for everything else |
| Exactly-once effect | After processing, dedup atomically with effect | Duplicates absorbed | Inbox / idempotency keys / Kafka txn | Money, inventory, counters, ledgers |

## In practice

### Runnable: duplicate deliveries against a wallet

A simulated broker crashes the consumer at random points. Four strategies process the same 201 messages (200 events plus one producer duplicate); only the inbox committed in the same transaction as the effect ends with the right balance.

```python
import random
import sqlite3
from collections import deque


class Crash(Exception):
    """The consumer process died at this point."""


def make_db():
    db = sqlite3.connect(":memory:", isolation_level=None)  # we issue BEGIN/COMMIT
    db.execute("CREATE TABLE wallet (id INTEGER PRIMARY KEY, balance INTEGER)")
    db.execute("CREATE TABLE inbox (message_id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO wallet VALUES (1, 0)")
    return db


def naive(db, msg, maybe_crash):
    db.execute("UPDATE wallet SET balance = balance + ? WHERE id = 1", (msg["amount"],))


def inbox_split_txn(db, msg, maybe_crash):
    # Dedup row commits on its own (autocommit), then the effect runs.
    if db.execute("INSERT OR IGNORE INTO inbox VALUES (?)", (msg["id"],)).rowcount:
        maybe_crash()  # dies after recording the id but before the effect
        db.execute("UPDATE wallet SET balance = balance + ? WHERE id = 1",
                   (msg["amount"],))


def inbox_same_txn(db, msg, maybe_crash):
    db.execute("BEGIN")
    try:
        if db.execute("INSERT OR IGNORE INTO inbox VALUES (?)", (msg["id"],)).rowcount:
            maybe_crash()
            db.execute("UPDATE wallet SET balance = balance + ? WHERE id = 1",
                       (msg["amount"],))
        db.execute("COMMIT")
    except Crash:
        db.execute("ROLLBACK")  # a dead process's transaction never commits
        raise


def run(messages, handler, ack_first=False, failure_rate=0.2, seed=7):
    rng = random.Random(seed)
    db, queue, deliveries = make_db(), deque(messages), 0

    def maybe_crash():
        if rng.random() < failure_rate:
            raise Crash()

    while queue:
        msg = queue.popleft()
        deliveries += 1
        try:
            if ack_first:          # at-most-once: broker forgets it now
                maybe_crash()      # crash before the effect = message lost
                handler(db, msg, lambda: None)
            else:                  # at-least-once: ack only after the effect
                handler(db, msg, maybe_crash)
                maybe_crash()      # crash after the effect, before the ack
        except Crash:
            if not ack_first:
                queue.append(msg)  # visibility timeout expires: redeliver
    balance = db.execute("SELECT balance FROM wallet").fetchone()[0]
    return balance, deliveries


if __name__ == "__main__":
    msgs = [{"id": f"evt-{i}", "amount": 10} for i in range(200)]
    msgs.insert(50, dict(msgs[7]))  # producer retried after a lost publish ack
    expected = 200 * 10

    results = {
        "at-most-once": run(msgs, naive, ack_first=True),
        "at-least-once naive": run(msgs, naive),
        "inbox, split txn": run(msgs, inbox_split_txn),
        "inbox, same txn": run(msgs, inbox_same_txn),
    }
    for name, (balance, deliveries) in results.items():
        print(f"{name:22} deliveries={deliveries:4} balance={balance} "
              f"(expected {expected})")

    assert results["at-most-once"][0] < expected          # lost messages
    assert results["at-least-once naive"][0] > expected   # double counted
    assert results["inbox, split txn"][0] < expected      # dedup turned dupes into loss
    assert results["inbox, same txn"][0] == expected      # exactly-once effect
    assert results["inbox, same txn"][1] > len(msgs)      # despite redeliveries
```

With seed 7 the balances come out at 1,520 (at-most-once), 2,620 (naive at-least-once), 1,520 (inbox in a separate transaction) and exactly 2,000 (inbox in the same transaction), even though the last one saw over 300 deliveries. The split-transaction inbox is the dangerous one: it looks idempotent in code review and loses money in production.

### Where SQS FIFO fits

SQS FIFO deduplicates *sends* that share a `MessageDeduplicationId` within a five-minute window. That removes producer-retry duplicates. It does not stop a consumer from processing a message twice when the visibility timeout expires mid-processing. For long jobs, extend visibility with a heartbeat (`ChangeMessageVisibility`) and keep the consumer idempotent anyway. See [RabbitMQ and SQS]({{ '/backend/rabbitmq-and-sqs/' | relative_url }}).

## Pitfalls

- **Dedup on the wrong key.** Using the broker's receipt handle or offset catches redelivery but not producer duplicates. Use a producer-assigned event id.
- **Dedup and effect in different transactions.** Converts duplicates into silent losses. Symptom: occasional missing effects with "already processed" in the logs.
- **Dedup window shorter than the replay window.** A DLQ redrive two weeks later re-applies everything. Keep ids for the maximum retention plus margin.
- **Visibility timeout shorter than worst-case processing.** Two consumers process the same message at the same time, and the dedup row only protects you if the unique constraint is checked inside the transaction. Size timeouts to p99.9 processing time, or heartbeat.
- **Kafka auto-commit with async processing.** `enable.auto.commit=true` commits offsets returned by earlier polls; if you handed records to a thread pool, they can be committed before they are processed. Commit manually after completion.
- **Assuming Kafka EOS covers the database.** It does not. Anything outside Kafka needs its own idempotency.
- **External side effects inside a retry loop.** Emails and webhooks cannot join your transaction. Record "sent" with the message id first, accept rare duplicates for low-stakes notifications, and use idempotency keys for payments.
- **Celery `acks_late` without idempotent tasks.** You switched from losing tasks to running them twice.

## Interview questions

**Can a message system guarantee exactly-once delivery?** Not to an arbitrary receiver over an unreliable network; a lost ack is indistinguishable from a lost message. Systems provide at-least-once delivery plus deduplication, which gives exactly-once effects within a defined scope.

**How do you make an increment idempotent?** Insert the message id into a processed-messages table with a unique constraint in the same transaction as the increment. If the insert conflicts, skip the increment and ack.

**A service commits to its database and then publishes an event. What can go wrong and how do you fix it?** A crash between the two loses the event; reversing the order publishes events for rolled-back writes. Use a transactional outbox: write the event to a table in the same transaction and relay it asynchronously, or capture it with CDC.

**What does Kafka's exactly-once actually guarantee?** Idempotent producers remove retry duplicates per partition within a producer session. Transactions make writes to multiple partitions plus the consumer offset commit atomic, and `read_committed` consumers skip aborted data. It covers Kafka-to-Kafka pipelines only.

**How long do you keep dedup records?** At least as long as a duplicate can arrive: broker retention, DLQ retention, and any replay you might run. Days to weeks, partitioned by time so cleanup is a partition drop.

**A consumer calls a payment provider. How do you avoid double charges?** Derive an idempotency key from the event id and send it with the request; record the outcome locally in the inbox transaction; reconcile against the provider's records for the rare unknown outcome (timeout after send).

**When is at-most-once the right choice?** When a newer message supersedes an older one and loss is invisible or cheap: metrics, heartbeats, location updates. It avoids dedup state and redelivery load.

## Further reading

- Apache Kafka documentation, "Message Delivery Semantics" (design section) and KIP-98, "Exactly Once Delivery and Transactional Messaging"
- Chris Richardson, *Microservices Patterns* (Manning, 2018): transactional outbox and idempotent consumer
- Amazon SQS Developer Guide: at-least-once delivery, FIFO exactly-once processing, visibility timeout
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 11 (fault tolerance and exactly-once)
- Tyler Treat, "You Cannot Have Exactly-Once Delivery" (Brave New Geek, 2015)
