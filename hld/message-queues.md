---
title: Message queues and streams
section: hld
order: 9
summary: Queue vs log, delivery guarantees, ordering, DLQs, the outbox pattern, and when to pick Kafka, RabbitMQ, SQS or a Postgres queue.
---

## What it is

Asynchronous communication through a broker: producers write messages, consumers process them later.

- **Queue (work distribution):** each message goes to one consumer, then is deleted on ack. RabbitMQ, SQS, Celery, Postgres queue.
- **Log / stream:** append-only, retained; consumers track an offset and can replay. Many consumer groups read the same data. Kafka, Kinesis, Redis Streams.

## When to use

- Decouple slow work from the request path (emails, thumbnails, embeddings).
- Absorb spikes (load leveling).
- Fan out one event to many services (stream).
- Retry failed work without the user waiting.
- Not for: request/response where the caller needs the result now.

## Delivery guarantees

| Guarantee | Meaning | How |
|---|---|---|
| At-most-once | May lose, never duplicates | Ack before processing |
| At-least-once | Never loses, may duplicate | Ack after processing; redeliver on timeout |
| Exactly-once | Effect happens once | At-least-once + **idempotent consumer** (or transactional sink, e.g. Kafka transactions within Kafka) |

> **Rule of thumb:** Assume at-least-once and make consumers idempotent. End-to-end exactly-once across systems is a myth without idempotency.

Idempotent consumer: store processed message ids (`INSERT ... ON CONFLICT DO NOTHING` in the same transaction as the side effect), or make the operation naturally idempotent (upsert, set state).

## Ordering

- Kafka: ordered **within a partition**. Use a key (`order_id`, `tenant_id`) so related events share a partition.
- SQS standard: no ordering. SQS FIFO: ordered per message group id, lower throughput.
- RabbitMQ: ordered per queue with one consumer; multiple consumers or redeliveries break it.
- Parallelism vs ordering: more partitions = more parallelism, ordering only per key.
- Retries break order. If order matters, block the key or use version numbers and discard stale events.

## Dead letter queue (DLQ)

- After N failed attempts, move the message to a DLQ instead of retrying forever (poison message).
- Alert on DLQ depth; provide a redrive tool after fixing the bug.
- Store the error and attempt count with the message.

## Outbox pattern

Problem: you write to the DB and publish an event. If one succeeds and the other fails, they diverge (dual write).

```sql
BEGIN;
UPDATE orders SET status = 'paid' WHERE id = 42;
INSERT INTO outbox (id, topic, key, payload)
VALUES (gen_random_uuid(), 'order.paid', '42', '{"order_id": 42}');
COMMIT;
```

- A relay (poller or CDC like Debezium) reads `outbox` and publishes, then marks rows sent.
- Gives at-least-once publishing; consumers dedupe by event id.
- Inbox pattern on the consumer side: record processed ids in the same transaction as the effect.

## Kafka vs RabbitMQ vs SQS vs Postgres queue

| | Kafka | RabbitMQ | SQS | Postgres queue |
|---|---|---|---|---|
| Model | Partitioned log | Broker queues, exchanges | Managed queue | Table + `SKIP LOCKED` |
| Retention / replay | Yes (days/forever) | No (deleted on ack) | No (max 14 days unconsumed) | Yes if you keep rows |
| Throughput | Very high (100K+ msg/s) | High (10K+/s) | Effectively unlimited (standard) | ~1K–10K jobs/s, DB-bound |
| Ordering | Per partition | Per queue | FIFO queues only | `ORDER BY` you choose |
| Routing | Topics only | Rich (topic, fanout, headers) | Basic; SNS for fanout | SQL |
| Ops cost | High (unless managed) | Medium | None | None extra |
| Transactional with app data | No (needs outbox) | No | No | **Yes**, same transaction |
| Best for | Event streaming, CDC, analytics pipelines, many consumers | Task queues with routing, RPC-ish patterns | AWS-native decoupling | Background jobs at moderate scale |

```sql
-- Postgres job claim
UPDATE jobs SET status = 'running', locked_until = now() + interval '5 min'
WHERE id = (
  SELECT id FROM jobs
  WHERE status = 'queued' AND run_at <= now()
  ORDER BY priority, run_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, payload;
```

## Gotchas

- **Consumer lag** is the key metric. Alert on lag and age of oldest message.
- **Visibility timeout** (SQS) or lease shorter than processing time causes duplicate processing.
- Large payloads: put the blob in object storage and send a reference.
- Kafka consumer group rebalances pause consumption; long processing between polls triggers them (`max.poll.interval.ms`).
- Postgres queues: dead tuples from high churn need aggressive autovacuum; keep the hot table small (move done jobs out).
- Backpressure: an unbounded queue hides an outage until it is hours deep.

> **Interview tip:** When you add a queue, say three things: delivery guarantee (at-least-once), how consumers are idempotent, and what happens to poison messages (DLQ).
