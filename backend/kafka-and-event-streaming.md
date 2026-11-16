---
title: Kafka and event streaming
section: backend
order: 42
phase: "Phase 6 · Messaging and events"
summary: Kafka's partitioned log, replication and ISR, producer acks and idempotence, retention and compaction, KRaft, stream processing and consumer groups.
---

Kafka is a distributed, replicated, append-only log. Producers append records to topics, records stay for a configured retention period whether or not anyone has read them, and each consumer group tracks its own position. That one design choice, keeping data instead of deleting it on delivery, is why Kafka is used for event streams many services read independently, for change data capture out of databases, for feeding analytics and search, and as the backbone of stream processing. It is also why it behaves differently from a task queue: ordering and parallelism are per partition, a slow record blocks its partition, and there is no per-message ack. For the queue-versus-log comparison and the general model see [Message queues and pub/sub]({{ '/backend/message-queue-fundamentals/' | relative_url }}).

## Apache Kafka

### Brokers, topics and partitions

A cluster is a set of brokers. A topic is split into partitions, and each partition is an ordered log stored as segment files on one broker's disk (plus replicas on others). Every record in a partition gets a sequential offset.

```text
 topic "orders": 3 partitions, replication factor 3
                                    offsets -->
 P0  [0][1][2][3][4][5][6]      leader: broker 1   <-- billing/c1
 P1  [0][1][2][3][4]            leader: broker 2   <-- billing/c2
 P2  [0][1][2][3][4][5]         leader: broker 3   <-- billing/c2
                 ^
     billing's committed offset on P2 = 4 (lag 2)

 group "billing":   c1 owns P0; c2 owns P1 and P2
 group "analytics": a1 owns P0, P1, P2 and reads the same data
```

The producer picks the partition. Records with a key go to `hash(key) % partitions` (murmur2 in the Java client), so every event for `order_id=812` lands in the same partition, in order. Records without a key are spread across partitions in sticky batches. Ordering is guaranteed only within a partition; there is no order across partitions.

Partitions are the unit of parallelism, storage placement and ordering. Choosing the count: estimate target throughput divided by what one consumer can process per partition, and leave room to grow. You can add partitions later but not remove them, and adding them changes which partition each key maps to, which breaks per-key ordering across the change. Very high partition counts cost memory, file handles and recovery time, so don't default to thousands.

Kafka is fast because of its storage design: sequential appends to segment files, reads served from the OS page cache, batching and compression end to end, and zero-copy transfer from file to socket.

### Replication and ISR

Each partition has one leader and `replication.factor - 1` followers on other brokers. Producers write to the leader; followers fetch from it like consumers. The in-sync replica set (ISR) is the leader plus followers that have caught up within `replica.lag.time.max.ms` (30 s by default in recent versions).

```text
 partition P0: replication.factor=3, min.insync.replicas=2
 broker 1 (leader)    [0 1 2 3 4 5 6 7]   in ISR
 broker 2 (follower)  [0 1 2 3 4 5 6 7]   in ISR
 broker 3 (follower)  [0 1 2 3 4]         lagging: dropped from ISR
 high watermark = 8: offsets 0-7 are committed and readable
```

A record is committed once every ISR member has it; the high watermark marks that point, and consumers only read below it. If the leader fails, the controller elects a new leader from the ISR, so committed records survive. `unclean.leader.election.enable=false` (the default) refuses to elect an out-of-sync replica, choosing unavailability over data loss.

`min.insync.replicas` sets how small the ISR can shrink before writes with `acks=all` are rejected with `NotEnoughReplicas`. The standard durable setup is replication factor 3, `min.insync.replicas=2`, `acks=all`: you survive one broker loss with no data loss and keep accepting writes.

### Producers: acks and idempotence

| `acks` | Leader responds when | Loss scenario |
|---|---|---|
| `0` | Never waits | Any failure |
| `1` | Leader has written it | Leader dies before followers copy it |
| `all` | All current ISR members have it | Only if fewer than `min.insync.replicas` survive |

Since Kafka 3.0 the producer defaults are `acks=all` and `enable.idempotence=true`. Idempotence solves the duplicate a retry would otherwise create: the producer gets a producer ID and numbers each batch per partition, and the broker drops a batch whose sequence number it has already written. It also preserves ordering across retries, with up to 5 in-flight requests per connection.

Transactions extend this. With a `transactional.id`, a producer can write to several partitions and commit consumer offsets atomically, so a read-process-write pipeline either publishes its outputs and advances its input offset, or does neither. Consumers set `isolation.level=read_committed` to skip aborted records. This is Kafka's exactly-once processing, and it applies only within Kafka; an external side effect such as an email or HTTP call still needs idempotency (see [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }})).

Throughput comes from batching: `linger.ms` waits a few milliseconds to fill a batch, `batch.size` caps it, and `compression.type` (`lz4` or `zstd`) compresses the whole batch.

### Retention and compaction

| `cleanup.policy` | Keeps | Use for |
|---|---|---|
| `delete` | Everything newer than `retention.ms` (default 7 days) or within `retention.bytes` | Event streams, logs, CDC with bounded replay |
| `compact` | At least the latest record per key | Current state: user profiles, config, changelogs |
| `compact,delete` | Latest per key, and nothing older than retention | Bounded state |

Retention deletes whole segments, so a record lives at least as long as the retention setting. Compaction rewrites old segments in the background, keeping only the newest value per key; a record with a null value (a tombstone) deletes the key after `delete.retention.ms`. A compacted topic works as a table you can rebuild state from, which is how Kafka stores consumer offsets (`__consumer_offsets`) and Kafka Streams stores state store changelogs. Tiered storage, available in recent versions, moves old segments to object storage so long retention doesn't need large broker disks.

### KRaft

Kafka used ZooKeeper for cluster metadata and controller election until KIP-500 replaced it with KRaft: a small quorum of controller nodes running Raft, storing metadata as an internal log (`__cluster_metadata`) that brokers replicate. KRaft became production-ready in 3.3, ZooKeeper mode was deprecated in 3.5, and Kafka 4.0 (2025) removed it. The gains: one system to operate, much faster controller failover, and support for far more partitions per cluster, since metadata no longer loads from ZooKeeper on failover. See [Leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}) for Raft itself.

## Event streaming

Event streaming treats data as an unbounded sequence of events processed continuously, instead of batches processed on a schedule. Kafka stores and transports the stream; a stream processor computes over it.

- **Stateless operations**: filter, map, route, enrich from a lookup. Each record is handled alone.
- **Stateful operations**: counts, aggregations, joins and deduplication. The processor keeps local state (often RocksDB) and backs it up to a changelog topic or checkpoint so it can recover.
- **Time**: event time (when it happened, a field in the record) versus processing time (when the processor saw it). Correct results need event time, which raises the question of late data. Watermarks estimate "all events before T have arrived", and a grace period decides how long a window accepts stragglers.

| Window | Shape | Example |
|---|---|---|
| Tumbling | Fixed size, no overlap | Orders per minute |
| Hopping or sliding | Fixed size, overlapping | 5-minute average updated every minute |
| Session | Gaps of inactivity close it | User browsing sessions with 30-minute timeout |

| | Kafka Streams | Apache Flink |
|---|---|---|
| Deployment | Library inside your JVM service | Separate cluster or managed service |
| Scaling | Run more app instances; one task per input partition | Job parallelism on task managers |
| State | Local RocksDB plus changelog topics | Managed state with distributed snapshots (checkpoints) |
| Sources | Kafka only | Kafka, files, databases, many connectors |
| Exactly-once | `processing.guarantee=exactly_once_v2` | Checkpoints plus transactional sinks |
| Fits | Event-driven microservices on Kafka | Large pipelines, complex event time, SQL |

Python teams usually reach Flink through PyFlink or Flink SQL, or use lighter libraries such as Quix Streams or Bytewax; check maturity before you commit. Around the core, Kafka Connect moves data in and out without custom code, and Debezium streams PostgreSQL changes from logical replication into Kafka, which is the usual way to get a reliable event stream out of an existing database. Stream processing also underpins [Event sourcing and CQRS]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }}) read models.

## Consumer groups

Consumers that share a `group.id` form a group, and the group divides the topic's partitions among its members. Each partition is consumed by exactly one member, so the group gets parallelism and per-partition ordering at once. Different groups are independent: each gets every record, which is how Kafka does pub/sub.

### Partition assignment

One partition goes to one member, so the partition count caps a group's parallelism: 12 partitions and 15 consumers leaves 3 idle. Assignors decide the mapping: range (contiguous blocks per topic), round-robin, sticky (balanced and minimal movement) and cooperative-sticky (sticky plus incremental rebalancing).

### Rebalancing

The group rebalances when a member joins, leaves, or is presumed dead: it missed heartbeats for `session.timeout.ms` (45 s by default since 3.0) or went longer than `max.poll.interval.ms` (5 minutes by default) between polls, which means processing is too slow. Partition count or subscription changes also trigger one.

- **Eager** (the original protocol): every member gives up all partitions, then gets a new assignment. Consumption stops group-wide for the duration.
- **Incremental cooperative** (KIP-429): only partitions that actually move are revoked; others keep flowing.
- **New consumer group protocol** (KIP-848, generally available in Kafka 4.0): the broker-side coordinator computes assignments and reconciles each member incrementally, removing the group-wide sync barrier. Enabled with `group.protocol=consumer`.
- **Static membership** (`group.instance.id`): a restarting pod rejoins with the same identity within the session timeout and gets its partitions back without a rebalance. Useful for rolling deploys.

### Offsets and commits

A committed offset is the next offset the group will read in a partition, stored in `__consumer_offsets`. After a rebalance or restart, the new owner resumes from it. When to commit decides the guarantee:

| Strategy | Crash between processing and commit | Result |
|---|---|---|
| Commit, then process | Records skipped | At-most-once |
| Process, then commit | Records reprocessed | At-least-once (the default choice) |
| Transactional read-process-write | Outputs and offsets commit atomically | Exactly-once within Kafka |

With `enable.auto.commit=true` the client commits periodically (every 5 s by default) the offsets returned by previous polls. That is at-least-once if you finish processing inside the poll loop, and can lose records if you hand them to another thread and poll again before they finish. Most production consumers disable it and commit after processing, per batch. `auto.offset.reset` (`earliest` or `latest`) decides where a group with no committed offset starts.

### Lag

Lag is log-end offset minus committed offset, per partition. Rising lag means consumers are slower than producers. Alert on lag that keeps growing or on time lag (how old the last committed record is), not on a fixed number, since a burst of a million small records may clear in seconds. Fix it by optimizing the handler, adding consumers up to the partition count, or adding partitions. Lag stuck on one partition points to a hot key or a poison record.

## In practice

A runnable toy: a partitioned log with key hashing, a consumer group with round-robin assignment, commits, a crash before commit, rebalance and lag.

```python
import zlib
from collections import defaultdict


class Topic:
    def __init__(self, partitions):
        self.logs = [[] for _ in range(partitions)]

    def produce(self, key, value):
        p = zlib.crc32(key.encode()) % len(self.logs)    # Kafka uses murmur2
        self.logs[p].append((key, value))
        return p, len(self.logs[p]) - 1                    # (partition, offset)


class ConsumerGroup:
    def __init__(self, topic):
        self.topic, self.members = topic, []
        self.committed = defaultdict(int)                  # partition -> next offset
        self.assignment, self.position = {}, {}

    def join(self, member):
        self.members.append(member)
        self.rebalance()

    def leave(self, member):                               # or crash: missed heartbeats
        self.members.remove(member)
        self.rebalance()

    def rebalance(self):
        members, n = sorted(self.members), len(self.topic.logs)
        self.assignment = {m: [p for p in range(n) if p % len(members) == i]
                           for i, m in enumerate(members)}
        self.position = {p: self.committed[p] for p in range(n)}   # resume from commits

    def poll(self, member, max_records=10):
        records = []
        for p in self.assignment[member]:
            pos = self.position[p]
            batch = self.topic.logs[p][pos:pos + max_records - len(records)]
            records += [(p, pos + i, k, v) for i, (k, v) in enumerate(batch)]
            self.position[p] = pos + len(batch)
        return records

    def commit(self, member):
        for p in self.assignment[member]:
            self.committed[p] = self.position[p]

    def lag(self):
        return {p: len(log) - self.committed[p] for p, log in enumerate(self.topic.logs)}


if __name__ == "__main__":
    orders = Topic(partitions=6)
    for i in range(60):
        orders.produce(f"customer-{i % 7}", i)

    group = ConsumerGroup(orders)
    group.join("c1")
    group.join("c2")
    owned = sorted(p for ps in group.assignment.values() for p in ps)
    assert owned == list(range(6))                         # each partition, one owner

    processed = defaultdict(list)
    def handle(records):
        for _, _, key, value in records:
            processed[key].append(value)

    handle(group.poll("c1", 100))
    group.commit("c1")
    crashed_batch = group.poll("c2", 5)
    handle(crashed_batch)                                  # c2 processes 5 records...
    assert len(crashed_batch) == 5
    group.leave("c2")                                      # ...and dies before committing

    while records := group.poll("c1", 100):                # c1 takes over c2's partitions
        handle(records)
        group.commit("c1")

    assert sorted(set(v for vs in processed.values() for v in vs)) == list(range(60))
    assert sum(map(len, processed.values())) == 65         # 5 duplicates: at-least-once
    for values in processed.values():
        first_seen = list(dict.fromkeys(values))
        assert first_seen == sorted(first_seen)            # per-key order holds
    assert all(lag == 0 for lag in group.lag().values())

    for i in range(2, 8):
        group.join(f"c{i}")                                # 7 members, 6 partitions
    assert sum(1 for ps in group.assignment.values() if not ps) == 1
    print("partitioned log ok")
```

Creating a topic and checking lag:

```bash
kafka-topics.sh --bootstrap-server kafka-1:9092 --create --topic orders \
  --partitions 12 --replication-factor 3 \
  --config min.insync.replicas=2 --config retention.ms=604800000

kafka-consumer-groups.sh --bootstrap-server kafka-1:9092 --describe --group billing
# GROUP    TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID ...
```

A durable producer and an at-least-once consumer with `confluent-kafka` (illustrative):

```python
from confluent_kafka import Consumer, Producer

producer = Producer({
    "bootstrap.servers": "kafka-1:9092,kafka-2:9092",
    "acks": "all",
    "enable.idempotence": True,
    "linger.ms": 10,
    "compression.type": "zstd",
})

def on_delivery(err, msg):
    if err is not None:
        logger.error("kafka delivery failed: %s", err)

producer.produce("orders", key=str(order.id), value=payload, on_delivery=on_delivery)
producer.poll(0)          # serve delivery callbacks; call producer.flush() on shutdown

consumer = Consumer({
    "bootstrap.servers": "kafka-1:9092",
    "group.id": "billing",
    "enable.auto.commit": False,
    "auto.offset.reset": "earliest",
    "partition.assignment.strategy": "cooperative-sticky",
})
consumer.subscribe(["orders"])
try:
    while running:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            logger.warning("consumer error: %s", msg.error())
            continue
        handle_order_event(msg.key(), msg.value())         # must be idempotent
        consumer.commit(message=msg, asynchronous=False)   # batch this in production
finally:
    consumer.close()      # leave the group so partitions move right away
```

## Pitfalls

- **Hot partitions.** One huge tenant or a constant key sends most traffic to one partition; lag grows there only. Fix: a better key (add a sub-key), or split hot tenants.
- **Adding partitions to a keyed topic.** Keys remap and per-key ordering breaks across the change. Size partitions upfront for a couple of years of growth.
- **Slow handlers trigger rebalance storms.** Processing a batch takes longer than `max.poll.interval.ms`, the consumer is kicked out, its partitions move, and the cycle repeats. Fix: smaller `max.poll.records`, faster handlers, or a bounded worker pool that pauses partitions while busy.
- **Auto-commit with async processing.** Offsets are committed for records still in flight, so a crash loses them.
- **Weak durability settings.** `acks=1` or `min.insync.replicas=1` with replication factor 3 can lose acknowledged writes when a leader dies.
- **Poison records.** A record that always fails blocks its partition forever. Retry a few times, then publish it to a dead-letter topic and move on (see [Ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }})).
- **Large messages.** Brokers default to roughly 1 MB per message; raising it hurts the whole cluster. Store blobs elsewhere and send references.
- **Treating Kafka as a task queue.** No per-message retry or delay, and parallelism capped by partitions. Kafka 4.x adds share groups (queue-style consumption, still early access or preview in 2025), but a real queue is still simpler for job dispatch.

## Interview questions

**How does Kafka guarantee ordering?** Only within a partition. Use a key so all events for one entity go to one partition, and keep idempotence on so retries don't reorder.

**What do `acks=all` and `min.insync.replicas` do together?** `acks=all` waits for every in-sync replica; `min.insync.replicas` rejects writes when the ISR is smaller than that number. With replication factor 3 and a minimum of 2, one broker can fail with no data loss and no write outage.

**How does the idempotent producer work?** A producer ID plus per-partition sequence numbers on each batch; the broker discards a batch it already wrote, so retries can't duplicate or reorder.

**What is a consumer group and how many consumers can it use?** Members sharing a `group.id` split partitions; each partition has one owner. Useful parallelism is at most the partition count.

**What causes rebalances and how do you reduce their impact?** Members joining, leaving, missing heartbeats or exceeding `max.poll.interval.ms`. Use cooperative-sticky assignment or the KIP-848 protocol, static membership for rolling restarts, and keep processing within the poll interval.

**Is Kafka exactly-once?** Within Kafka, yes: idempotent producers plus transactions and `read_committed` consumers make read-process-write atomic. Side effects outside Kafka need idempotent handlers.

**Delete versus compact retention?** Delete drops segments older than the retention time. Compact keeps the latest value per key, turning the topic into a rebuildable table; tombstones remove keys.

**What is consumer lag and what do you do about it?** Log-end offset minus committed offset. Alert on growth or time lag; fix with faster handlers, more consumers up to the partition count, or more partitions. Lag on one partition suggests a hot key or poison record.

## Further reading

- "Kafka: a Distributed Messaging System for Log Processing" (Kreps, Narkhede and Rao, NetDB 2011).
- Apache Kafka documentation, the Design and Operations sections.
- *Kafka: The Definitive Guide* (Shapira, Palino, Sivaram and Petty, 2nd ed., O'Reilly).
- *Streaming Systems* (Akidau, Chernyak and Lax, O'Reilly), on event time, windows and watermarks.
- KIP-500 and KIP-848 on the Apache Kafka wiki, for KRaft and the new consumer group protocol.
