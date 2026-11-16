---
title: RabbitMQ and SQS
section: backend
order: 43
phase: "Phase 6 · Messaging and events"
summary: RabbitMQ exchanges, bindings, acks, prefetch, quorum queues and DLX; SQS standard vs FIFO, visibility, long polling and redrive; and when Kafka fits instead.
---

RabbitMQ and SQS are the two queues a Python backend most often meets, usually underneath Celery. RabbitMQ is a broker you run (or rent) with a flexible routing model: producers publish to exchanges, and bindings decide which queues get a copy. SQS is a fully managed AWS queue with almost no configuration, no connection state and practically unlimited scale, at the cost of a simpler feature set. Both are queues in the sense of [Message queues and pub/sub]({{ '/backend/message-queue-fundamentals/' | relative_url }}): messages are deleted when acknowledged, and retries happen per message. Kafka is a log and makes different trade-offs; the comparison is at the end.

## RabbitMQ

RabbitMQ is an Erlang broker built around AMQP 0-9-1 (it also speaks AMQP 1.0, MQTT and STOMP). Producers never write to a queue directly. They publish to an exchange with a routing key, the exchange routes the message to zero or more queues according to bindings, and consumers read from queues.

```text
                      exchange "orders"     binding key          queue
 publish              +---------------+
 key=order.eu.created |  type: topic  |--- "order.*.created" --> billing
 -------------------> |               |--- "order.eu.#" -------> eu-ops
                      |               |--- "#" ----------------> audit
                      +---------------+--- "order.*.refunded"    refunds
                                           (no match, no copy)

 unroutable and mandatory=true -> returned to the producer
 rejected, expired, over limit -> dead-letter exchange -> DLQ
```

### Exchanges

| Type | Routes to queues whose binding... | Typical use |
|---|---|---|
| Direct | Key equals the routing key exactly | Task queues by name, per-type routing |
| Topic | Pattern matches the dot-separated key: `*` is exactly one word, `#` is zero or more | Events by entity, region, type |
| Fanout | Anything; the key is ignored | Broadcast to every bound queue |
| Headers | Matches message headers, with `x-match` = `all` or `any` | Routing on several attributes |

Every broker also has a nameless default exchange, a direct exchange where each queue is automatically bound by its own name. Publishing to `""` with routing key `emails` puts the message in the `emails` queue, which is what most "send to a queue" examples (and Celery's defaults) use. A queue receives at most one copy of a message even if several of its bindings match.

### Queues and bindings

A binding links an exchange to a queue with a binding key (or header arguments). Queues have properties: durable (survives a broker restart), exclusive (one connection, deleted when it closes) and auto-delete (deleted when the last consumer leaves). Messages survive a restart only if the queue is durable and the message is persistent (`delivery_mode=2`); quorum queues always persist. Configure TTLs, length limits and dead-lettering with policies rather than per-queue arguments where possible, since policies can be changed without redeclaring queues.

### Acks and publisher confirms

Consumers ack with `basic.ack`, or reject with `basic.nack` or `basic.reject` and choose `requeue=True` (back to the queue) or `requeue=False` (dead-lettered if a DLX is set, otherwise dropped). Unacked messages are redelivered with the `redelivered` flag when the consumer's channel or connection closes. RabbitMQ also enforces a consumer delivery timeout (30 minutes by default in recent versions): a consumer holding an unacked message longer than that has its channel closed.

The other direction matters as much. With publisher confirms enabled, the broker acks each published message once it has been written (for quorum queues, once a majority of replicas have it). Without confirms, a publish into a broker that crashes a moment later is silently lost. Set the `mandatory` flag to get unroutable messages returned instead of dropped.

### Prefetch

`basic.qos(prefetch_count=N)` limits how many unacked messages the broker pushes to a consumer. RabbitMQ's guidance puts the useful range for fast consumers in the tens to a few hundred. Use 1 for long or uneven tasks so one slow message doesn't hold others hostage. Celery maps this to `worker_prefetch_multiplier × concurrency`.

### Quorum queues

Classic queues live on one node. Quorum queues replicate each queue across an odd number of nodes (usually 3 or 5) with Raft; a message is confirmed once a majority has it, and the queue stays available while a majority of its replicas are up. They replaced classic mirrored queues, which were deprecated and then removed in RabbitMQ 4.0. Quorum queues also track deliveries per message and enforce a delivery limit (`x-delivery-limit`; RabbitMQ 4.0 sets a default of 20), so a poison message is eventually dead-lettered instead of looping forever. Use them for anything you can't afford to lose. RabbitMQ also has streams, an append-only replicated log with non-destructive reads and offsets, for Kafka-like replay inside RabbitMQ.

### Dead letter exchanges

A queue with `x-dead-letter-exchange` set republishes a message to that exchange when it is:

- rejected or nacked with `requeue=False`,
- expired by a per-message or per-queue TTL,
- dropped because the queue exceeded its length limit,
- delivered more times than a quorum queue's delivery limit.

The message carries an `x-death` header recording why and from where. Combining TTL and DLX gives delayed retries without a plugin: failed messages go to a `billing.retry.30s` queue with a 30 s TTL and no consumers, whose DLX points back at the main exchange. After N rounds (read from `x-death`), route to a parking-lot queue for humans. See [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}).

## AWS SQS

SQS is a managed queue accessed over HTTPS. There are no connections, exchanges or bindings: you send to a queue URL, receive with polling, and delete with a receipt handle. For fan-out, publish to SNS (or EventBridge) and subscribe one SQS queue per consumer.

### Standard versus FIFO

| | Standard | FIFO (name ends in `.fifo`) |
|---|---|---|
| Throughput | Nearly unlimited | Per-queue limits; 300 API calls/s per action without batching, much higher in high-throughput mode |
| Delivery | At-least-once; occasional duplicates | Deduplicated within a 5-minute window |
| Ordering | Best effort | Strict within a message group |
| Dedup key | None | `MessageDeduplicationId` or content-based |

Default to standard and make consumers idempotent. Use FIFO when per-entity order matters and throughput fits the limits, which AWS has raised several times; check current quotas. The payload limit is small (256 KiB for most of SQS's history, raised to 1 MiB in 2025), so larger payloads go to S3 with a pointer in the message.

### Visibility timeout

A received message is hidden for the visibility timeout (default 30 s, up to 12 hours) and returns to the queue if not deleted in time. Each receive returns a new receipt handle; delete or extend with the latest one. For long jobs call `ChangeMessageVisibility` periodically from a heartbeat. When a Lambda function consumes the queue, AWS recommends a visibility timeout of at least six times the function timeout. Messages are retained for 4 days by default, configurable from 1 minute to 14 days.

### Long polling

A short-poll `ReceiveMessage` queries a subset of SQS's servers and returns immediately, often empty even when messages exist. Long polling (`WaitTimeSeconds` up to 20, or the queue attribute `ReceiveMessageWaitTimeSeconds`) waits for messages to arrive and queries all servers. It cuts empty responses, which you pay for, and latency. Always use it; receive up to 10 messages per call and delete in batches.

### DLQ and redrive

A redrive policy on the source queue names a dead-letter queue and a `maxReceiveCount`. After that many receives without a delete, SQS moves the message to the DLQ. The DLQ must be the same type (FIFO for FIFO) in the same account and region. After fixing the bug, redrive moves messages back to the source queue from the console or with `StartMessageMoveTask`. For standard queues, a message's retention clock starts at its original enqueue time, so make the DLQ's retention longer than the source queue's, or messages expire soon after arriving there.

### Message groups

In a FIFO queue, `MessageGroupId` is the unit of ordering. Messages in one group are delivered in order, and while one batch from a group is in flight no other consumer receives messages from that group. Different groups proceed in parallel. Use the entity ID (`order_id`, `account_id`) as the group: you get per-entity ordering with parallelism across entities. A hot group serializes behind itself, and a failing message blocks its group until it succeeds or reaches the DLQ. As of 2025, standard queues also accept a message group ID for fair queuing, which keeps one noisy tenant from delaying the others without imposing ordering.

## Comparison with Kafka

| | RabbitMQ | SQS | Kafka |
|---|---|---|---|
| Model | Queue with routing | Managed queue | Partitioned log |
| Routing | Exchanges and bindings | None; fan-out via SNS or EventBridge | Topics; consumers filter |
| Consumption | Push with prefetch | Pull with long polling | Pull by offset |
| Acks | Per message | Per message (delete) | Committed offset per partition |
| Per-message retry and delay | Yes (requeue, TTL + DLX) | Yes (visibility, DLQ) | No; retry topics by hand |
| Ordering | Per queue, single consumer | FIFO per message group | Per partition |
| Replay | No (except streams) | No | Yes, within retention |
| Throughput | Tens of thousands of msg/s per node | Very high (standard) | Millions of msg/s per cluster |
| Operations | Run a cluster, or hosted | None | Run a cluster, or managed |
| Fits | Task queues, complex routing, RPC | Task queues on AWS, decoupling | Event streams, CDC, analytics, replay |

The rule of thumb: if the message is a job to be done once (send this email, transcode this video), use a queue; RabbitMQ when you need routing or run outside AWS, SQS when you are on AWS and want zero operations. If the message is a fact that several systems read now and later (order placed, row changed), use a log such as [Kafka]({{ '/backend/kafka-and-event-streaming/' | relative_url }}).

## In practice

A runnable model of RabbitMQ routing: topic pattern matching and the four exchange types.

```python
def topic_match(pattern, routing_key):
    """'*' matches exactly one word, '#' matches zero or more words."""
    def match(p, k):
        if not p:
            return not k
        if p[0] == "#":
            return any(match(p[1:], k[i:]) for i in range(len(k) + 1))
        return bool(k) and p[0] in ("*", k[0]) and match(p[1:], k[1:])
    return match(pattern.split("."), routing_key.split("."))


def headers_match(args, headers):
    wanted = {k: v for k, v in args.items() if not k.startswith("x-")}
    hits = [headers.get(k) == v for k, v in wanted.items()]
    return any(hits) if args.get("x-match") == "any" else all(hits)


class Exchange:
    def __init__(self, kind):
        self.kind, self.bindings = kind, []          # (queue, binding key or header args)

    def bind(self, queue, key=None):
        self.bindings.append((queue, key))

    def route(self, routing_key="", headers=None):
        matched = [
            queue for queue, key in self.bindings
            if self.kind == "fanout"
            or (self.kind == "direct" and key == routing_key)
            or (self.kind == "topic" and topic_match(key, routing_key))
            or (self.kind == "headers" and headers_match(key, headers or {}))
        ]
        return list(dict.fromkeys(matched))          # one copy per queue


if __name__ == "__main__":
    assert topic_match("order.*.created", "order.eu.created")
    assert not topic_match("order.*.created", "order.eu.de.created")
    assert topic_match("order.#.created", "order.eu.de.created")
    assert topic_match("order.#", "order")           # '#' can match zero words
    assert not topic_match("order.*", "order")       # '*' needs exactly one
    assert topic_match("#", "user.signup")

    events = Exchange("topic")
    events.bind("billing", "order.*.created")
    events.bind("eu-ops", "order.eu.#")
    events.bind("audit", "#")
    events.bind("audit", "order.#")                  # overlapping binding, still one copy
    assert events.route("order.eu.created") == ["billing", "eu-ops", "audit"]
    assert events.route("order.us.refunded") == ["audit"]

    tasks = Exchange("direct")
    tasks.bind("emails", "email")
    tasks.bind("thumbnails", "image")
    assert tasks.route("image") == ["thumbnails"] and tasks.route("pdf") == []

    broadcast = Exchange("fanout")
    broadcast.bind("cache-node-1")
    broadcast.bind("cache-node-2")
    assert broadcast.route("ignored") == ["cache-node-1", "cache-node-2"]

    docs = Exchange("headers")
    docs.bind("urgent-pdf", {"x-match": "all", "format": "pdf", "priority": "high"})
    docs.bind("any-pdf-or-de", {"x-match": "any", "format": "pdf", "lang": "de"})
    assert docs.route(headers={"format": "pdf", "priority": "low"}) == ["any-pdf-or-de"]
    assert docs.route(headers={"format": "pdf", "priority": "high"}) == ["urgent-pdf", "any-pdf-or-de"]
    print("routing ok")
```

Declaring a topic exchange, a quorum queue with a DLX, publisher confirms and a prefetch-limited consumer with `pika` (illustrative):

```python
import pika

conn = pika.BlockingConnection(pika.URLParameters("amqps://app:secret@mq.internal/"))
ch = conn.channel()
ch.exchange_declare(exchange="orders", exchange_type="topic", durable=True)
ch.exchange_declare(exchange="orders.dlx", exchange_type="fanout", durable=True)
ch.queue_declare(queue="billing.dead", durable=True, arguments={"x-queue-type": "quorum"})
ch.queue_bind(queue="billing.dead", exchange="orders.dlx")
ch.queue_declare(queue="billing", durable=True, arguments={
    "x-queue-type": "quorum",
    "x-dead-letter-exchange": "orders.dlx",
    "x-delivery-limit": 5,
})
ch.queue_bind(queue="billing", exchange="orders", routing_key="order.*.created")

ch.confirm_delivery()                    # basic_publish now raises if the broker nacks
ch.basic_publish(
    exchange="orders", routing_key="order.eu.created", body=payload, mandatory=True,
    properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
)

def on_message(channel, method, properties, body):
    try:
        bill_order(body)                 # idempotent on order_id
        channel.basic_ack(delivery_tag=method.delivery_tag)
    except PermanentError:
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)   # to DLX

ch.basic_qos(prefetch_count=20)
ch.basic_consume(queue="billing", on_message_callback=on_message)
ch.start_consuming()
```

An SQS worker loop with long polling and batch deletes, plus the redrive policy (illustrative, `boto3`):

```python
import json

import boto3

sqs = boto3.client("sqs")
sqs.set_queue_attributes(QueueUrl=QUEUE_URL, Attributes={
    "RedrivePolicy": json.dumps({"deadLetterTargetArn": DLQ_ARN, "maxReceiveCount": "5"}),
    "ReceiveMessageWaitTimeSeconds": "20",
    "VisibilityTimeout": "120",
})

while running:
    resp = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20)
    done = []
    for m in resp.get("Messages", []):
        try:
            handle(json.loads(m["Body"]))      # idempotent: standard queues duplicate
            done.append({"Id": m["MessageId"], "ReceiptHandle": m["ReceiptHandle"]})
        except Exception:
            logger.exception("will retry after the visibility timeout")
    if done:
        sqs.delete_message_batch(QueueUrl=QUEUE_URL, Entries=done)

# FIFO: order per order_id, parallel across orders
sqs.send_message(QueueUrl=FIFO_URL, MessageBody=body,
                 MessageGroupId=str(order_id), MessageDeduplicationId=event_id)
```

## Pitfalls

- **No publisher confirms.** RabbitMQ publishes that look successful are lost when a node fails. Enable confirms and handle nacks.
- **Requeue loops.** `basic_nack(requeue=True)` on a poison message redelivers it instantly and forever. Use `requeue=False` with a DLX, and quorum queue delivery limits.
- **Classic queues for critical data.** A single node failure loses or blocks them. Use quorum queues.
- **Too many queues or connections.** RabbitMQ handles many, but a connection per request or a queue per user burns memory. Reuse long-lived connections, one channel per thread.
- **SQS short polling.** Empty receives cost money and add latency. Set `WaitTimeSeconds=20`.
- **Visibility timeout below processing time.** Duplicate concurrent processing, and receive counts reaching the DLQ for messages that were actually working.
- **DLQ retention too short.** Standard-queue messages keep their original enqueue time and can expire right after landing in the DLQ.
- **One hot FIFO message group.** All its messages serialize; a failing message blocks the group. Choose fine-grained groups and redrive poison messages quickly.

## Interview questions

**Explain RabbitMQ's routing model.** Producers publish to an exchange with a routing key. Bindings connect exchanges to queues; direct matches the key exactly, topic matches dotted patterns with `*` and `#`, fanout copies to all bound queues, headers matches header values. Consumers read from queues.

**How do you avoid losing messages in RabbitMQ?** Durable quorum queues, persistent messages, publisher confirms with the `mandatory` flag, manual consumer acks after processing, and a DLX for rejects.

**What is a quorum queue?** A Raft-replicated queue across an odd number of nodes, confirmed on majority write and available while a majority is up, with per-message delivery limits for poison messages.

**SQS standard versus FIFO?** Standard has near-unlimited throughput, at-least-once delivery and best-effort order. FIFO orders within a message group, deduplicates within 5 minutes, and has throughput quotas.

**What happens if a consumer crashes mid-message in SQS?** Nothing until the visibility timeout expires; then the message is visible again and another consumer receives it. After `maxReceiveCount` receives it moves to the DLQ.

**How do you get per-customer ordering with parallelism?** RabbitMQ: consistent-hash routing to several queues with one consumer each. SQS FIFO: `MessageGroupId` = customer ID. Kafka: key by customer ID.

**When would you choose Kafka over RabbitMQ or SQS?** When events must be retained and replayed, read by many independent consumers, or processed as streams at high volume. For one-off jobs with per-message retries and delays, a queue is simpler.

## Further reading

- RabbitMQ documentation: "AMQP 0-9-1 Model Explained", "Quorum Queues", "Dead Letter Exchanges", "Consumer Acknowledgements and Publisher Confirms".
- Amazon SQS Developer Guide: visibility timeout, long polling, FIFO queues and dead-letter queues.
- *RabbitMQ in Depth* (Gavin M. Roy, Manning).
- *Enterprise Integration Patterns* (Gregor Hohpe and Bobby Woolf).
