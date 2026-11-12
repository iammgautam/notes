---
title: Design a pub-sub message broker
section: lld
order: 44
phase: "Phase 5 · Advanced"
summary: A Kafka-style in-memory broker with partitioned append-only logs, key-based partitioning, consumer groups, offset commits, at-least-once delivery and retention.
---

A pub-sub broker decouples producers from consumers. The Kafka model is the one interviewers expect: a topic is split into partitions, each partition is an append-only log, and consumers track their own position (offset) in it. Ordering, scaling and redelivery all fall out of that one idea.

## Requirements

Clarifying questions:
- Queue semantics (each message to one consumer) or pub-sub (every subscriber group gets every message)? Both: each group gets every message; within a group, each partition goes to one member.
- Ordering guarantee? Per partition only. Messages with the same key go to the same partition, so per-key order holds.
- Delivery guarantee? At-least-once: consumers commit after processing, so a crash causes redelivery, never loss.
- Push or pull? Pull: consumers poll at their own pace.
- Retention? Time-based; consumed or not, old messages are deleted.

Functional:
- Create topics with N partitions.
- Publish with an optional key: same key → same partition; no key → round robin.
- Consumer groups: members join and leave; partitions are rebalanced across members.
- Poll returns messages from the member's partitions starting at its position; commit saves positions.
- Retention deletes messages older than a limit.

Non-functional:
- Thread-safe: many producers and consumers at once.
- Appends are `O(1)`; reads by offset are `O(1)` to locate.
- In-memory only for the interview.

Out of scope: replication, disk segments, exactly-once transactions, compaction.

## Entities and relationships

```text
+-------------------+ 1    * +------------------+ 1    * +-----------+
| Broker            |------->| Topic            |------->| Partition |
| topics {name: T}  |        | name             |        | log [Msg] |
| groups {(g,t): G} |        | partitions [P]   |        | start     |
| publish(t,v,key)  |        | partition_for()  |        | lock      |
| group(g, t)       |        +------------------+        | append()  |
| apply_retention() |                                    | read()    |
+---------+---------+                                    +-----------+
          | 1..*
+---------v---------------+  1    *  +-------------------------+
| ConsumerGroup           |<---------| Consumer                |
| members, generation     |  joins   | member_id, generation   |
| assignment {m: [p]}     |          | positions {p: offset}   |
| committed {p: offset}   |          | poll(), commit(), close |
| join/leave, commit()    |          +-------------------------+
+-------------------------+
Message(topic, partition, offset, key, value, ts) is immutable.
```

## Patterns and why

| Pattern | Where | Why |
|---|---|---|
| Publish-subscribe (Observer at system level) | Producers and groups only know topics | Producers never learn who consumes; new groups need no producer change |
| Append-only log | `Partition.log` | Ordering is the offset; replay is just reading from an older offset |
| Strategy (partitioner, assignor) | `partition_for`, `_rebalance` | Key hashing and range/round-robin assignment are swappable policies |
| Fencing token | `generation` on commit | A consumer that lost its partitions can't overwrite the new owner's offsets |

## Code

```python
from __future__ import annotations

import itertools
import threading
import time
import zlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: str
    ts: float


class Partition:
    def __init__(self, topic: str, index: int):
        self.topic, self.index = topic, index
        self.log: list[Message] = []
        self.start = 0  # offset of log[0]; moves forward as retention deletes
        self._lock = threading.Lock()

    def append(self, key: str | None, value: str) -> int:
        with self._lock:  # offset assignment + append must be one step
            offset = self.start + len(self.log)
            self.log.append(Message(self.topic, self.index, offset, key, value, time.time()))
            return offset

    def read(self, offset: int, max_n: int) -> list[Message]:
        with self._lock:
            i = max(offset - self.start, 0)  # below retention: resume at earliest
            return self.log[i : i + max_n]

    def apply_retention(self, max_age_s: float, now: float) -> None:
        with self._lock:
            # Simplification: O(n) front delete; real brokers drop whole segment files
            cut = 0
            while cut < len(self.log) and now - self.log[cut].ts > max_age_s:
                cut += 1
            del self.log[:cut]
            self.start += cut


class Topic:
    def __init__(self, name: str, partitions: int):
        self.name = name
        self.partitions = [Partition(name, i) for i in range(partitions)]
        self._rr = itertools.count()

    def partition_for(self, key: str | None) -> int:
        if key is None:
            return next(self._rr) % len(self.partitions)
        # crc32 is stable across processes; built-in hash() of str is salted per run
        return zlib.crc32(key.encode()) % len(self.partitions)


class StaleGeneration(Exception):
    pass


class ConsumerGroup:
    def __init__(self, topic: Topic):
        self.topic = topic
        self.members: list[str] = []
        self.assignment: dict[str, list[int]] = {}
        self.committed: dict[int, int] = {}
        self.generation = 0
        self._lock = threading.Lock()

    def join(self, member: str) -> None:
        with self._lock:
            if member not in self.members:
                self.members.append(member)
                self._rebalance()

    def leave(self, member: str) -> None:
        with self._lock:
            if member in self.members:
                self.members.remove(member)
                self._rebalance()

    def _rebalance(self) -> None:  # round-robin assignor
        self.generation += 1
        ms, n = sorted(self.members), len(self.topic.partitions)
        self.assignment = {m: list(range(i, n, len(ms))) for i, m in enumerate(ms)}

    def owned(self, member: str) -> tuple[int, dict[int, int]]:
        with self._lock:
            parts = self.assignment.get(member, [])
            return self.generation, {p: self.committed.get(p, 0) for p in parts}

    def commit(self, member: str, generation: int, offsets: dict[int, int]) -> None:
        with self._lock:
            if generation != self.generation:
                raise StaleGeneration("rebalanced since your last poll; rejoin")
            mine = set(self.assignment.get(member, []))
            self.committed.update((p, o) for p, o in offsets.items() if p in mine)


class Broker:
    def __init__(self):
        self.topics: dict[str, Topic] = {}
        self.groups: dict[tuple[str, str], ConsumerGroup] = {}
        self._lock = threading.Lock()

    def create_topic(self, name: str, partitions: int) -> Topic:
        with self._lock:
            return self.topics.setdefault(name, Topic(name, partitions))

    def publish(self, topic: str, value: str, key: str | None = None) -> tuple[int, int]:
        t = self.topics[topic]
        p = t.partition_for(key)
        return p, t.partitions[p].append(key, value)

    def group(self, group_id: str, topic: str) -> ConsumerGroup:
        with self._lock:
            return self.groups.setdefault((group_id, topic), ConsumerGroup(self.topics[topic]))

    def apply_retention(self, max_age_s: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for t in list(self.topics.values()):
            for p in t.partitions:
                p.apply_retention(max_age_s, now)


class Consumer:
    """Not thread-safe by design: one consumer object per thread (like Kafka's)."""

    def __init__(self, broker: Broker, group_id: str, topic: str, member_id: str):
        self.topic = broker.topics[topic]
        self.group = broker.group(group_id, topic)
        self.member_id, self.generation = member_id, -1
        self.positions: dict[int, int] = {}
        self.group.join(member_id)

    def poll(self, max_n: int = 100) -> list[Message]:
        gen, committed = self.group.owned(self.member_id)
        if gen != self.generation:  # rebalanced: restart from committed offsets
            self.generation, self.positions = gen, committed
        out: list[Message] = []
        for p, pos in self.positions.items():
            batch = self.topic.partitions[p].read(pos, max_n)
            if batch:
                self.positions[p] = batch[-1].offset + 1
                out.extend(batch)
        return out

    def commit(self) -> None:  # call after processing: at-least-once
        self.group.commit(self.member_id, self.generation, dict(self.positions))

    def close(self) -> None:
        self.group.leave(self.member_id)


if __name__ == "__main__":
    broker = Broker()
    broker.create_topic("orders", partitions=3)

    def produce(tid: int) -> None:
        for n in range(250):
            broker.publish("orders", f"{tid}:{n}", key=f"user-{tid}")

    threads = [threading.Thread(target=produce, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    topic = broker.topics["orders"]
    for part in topic.partitions:  # offsets dense; per-key order preserved
        assert [m.offset for m in part.log] == list(range(len(part.log)))
        for tid in range(4):
            seq = [int(m.value.split(":")[1]) for m in part.log if m.key == f"user-{tid}"]
            assert seq in ([], list(range(250)))

    # one group, 2 consumers in threads; the second join triggers a rebalance
    seen, seen_lock = set(), threading.Lock()

    def consume(member: str) -> None:
        c = Consumer(broker, "billing", "orders", member)
        deadline = time.time() + 5
        while time.time() < deadline:
            batch = c.poll(50)
            with seen_lock:
                seen.update((m.partition, m.offset) for m in batch)
                if len(seen) >= 1000:
                    break
            try:
                c.commit()
            except StaleGeneration:
                pass  # next poll picks up the new assignment

    workers = [threading.Thread(target=consume, args=(f"c{i}",)) for i in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert len(seen) == 1000  # nothing lost; duplicates after a rebalance are allowed

    # at-least-once: crash before commit -> the next owner gets it again
    a = Consumer(broker, "audit", "orders", "a")
    first = a.poll(5)
    a.commit()
    uncommitted = a.poll(5)
    a.close()  # crash: second batch processed but never committed
    b = Consumer(broker, "audit", "orders", "b")
    again = b.poll(5)
    assert {(m.partition, m.offset) for m in uncommitted} <= {(m.partition, m.offset) for m in again}
    assert not {(m.partition, m.offset) for m in first} & {(m.partition, m.offset) for m in again}
    try:  # zombie "a" can't commit offsets for partitions it no longer owns
        a.commit()
        raise AssertionError("stale commit accepted")
    except StaleGeneration:
        pass

    # retention: everything is older than 0 s an hour from now
    broker.apply_retention(max_age_s=0, now=time.time() + 3600)
    assert all(not p.log for p in topic.partitions)
    p, off = broker.publish("orders", "late", key="user-0")
    assert off == topic.partitions[p].start  # offsets never reset after deletion
    assert [m.value for m in b.poll(100)] == ["late"]  # resumes at earliest
    print("ok")
```

## Key flows

Publish with a key, then consume and commit:

```text
Producer      Broker             Partition 1    Consumer        Group
  | publish(v,k) |                   |              |             |
  |------------->| p = crc32(k)%3    |              |             |
  |              |-- append -------->| offset 41    |             |
  |<- (1, 41) ---|                   |              |             |
  |              |                   |              |-- owned() ->|
  |              |                   |              |<- gen,{1:41}|
  |              |                   |<- read(41) --|             |
  |              |                   |--- [m41] --->|             |
  |              |                   |              | process m41 |
  |              |                   |              |-- commit -->|
  |              |                   |              | gen,{1:42}  |
  |              |                   |              |<---- ok ----|
```

Rebalance: member joins or leaves → generation + 1 → each consumer's next `poll()` sees the new generation and restarts from committed offsets → a stale member's `commit()` raises `StaleGeneration`.

## Edge cases

- More consumers than partitions: extra members get no partitions and sit idle.
- Committed offset older than retention: `read` resumes at the earliest retained offset (Kafka's `auto.offset.reset=earliest`).
- Producer sends no key: round robin, so no ordering across those messages.
- Consumer crashes after processing but before commit: the batch is redelivered; consumers must be idempotent.
- Poll on a brand-new group: starts at offset 0 (earliest). Kafka lets you choose latest instead.
- Changing the partition count remaps keys (`crc32 % n` changes), breaking per-key order for in-flight keys.

## Production follow-ups

**Thread safety / concurrency?** Each partition has its own lock, so producers to different partitions never contend and offset assignment plus append is one atomic step. Group membership and commits are guarded by the group lock. A `Consumer` object is single-threaded on purpose (Kafka's is too); scale consumption by adding members, not threads per member. See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

**Persistence and restart?** Write each partition as segment files on disk (append, fsync on a policy), with a sparse offset index per segment so a read seeks quickly. Committed offsets are themselves messages in an internal compacted topic, which is how Kafka survives a broker restart without a separate database.

**Idempotency and retries?** Consumers dedupe on `(partition, offset)` or a message id stored with their side effect in the same transaction. Producers that retry after a timeout can write duplicates; fix with a producer id plus per-partition sequence number that the broker checks (Kafka's idempotent producer).

**Extensibility?** Partitioners (sticky, custom by tenant) and assignors (range, round robin, sticky) are strategies; a new one is a new function, not an edit to the log. Delivery semantics change by when you commit: before processing gives at-most-once, after gives at-least-once.

**Scale (10×, across machines)?** Partitions are the unit of parallelism and placement: spread them across brokers, replicate each to followers, and let a controller pick a leader per partition. Producers write to the leader and wait for in-sync replicas (`acks=all`) before acking. Add partitions up front because changing the count remaps keys.

**Observability?** Consumer lag per partition (end offset minus committed offset) is the metric that matters most; alert when it grows. Also produce and fetch rates, rebalance count and duration, retention bytes deleted, and stale-generation commit errors.

**Backpressure and slow consumers?** A pull model gives natural backpressure: a slow consumer just falls behind, and lag shows it. Retention bounds memory; if lag exceeds retention, data is lost to that consumer, so alert well before that point.

**Exactly-once?** At-least-once delivery plus idempotent processing is the practical answer. True exactly-once needs atomic "write output + commit offset" (Kafka transactions, or storing offsets in your own database transaction).

## Interview follow-ups

- **Why partitions instead of one queue per topic?** One log means one consumer per group and one machine's throughput. Partitions let a group read in parallel while keeping order within each partition.
- **How is per-key ordering guaranteed?** Same key → same partition via a stable hash, and one partition is read by only one group member at a time.
- **What happens during a rebalance?** Generation bumps, consumers restart from committed offsets, uncommitted work is redelivered, and old members are fenced from committing.
- **How do you choose the partition count?** Target throughput divided by per-consumer throughput, rounded up with headroom, because increasing it later remaps keys.
- **Push vs pull?** Pull lets each consumer go at its own speed and batch reads; push needs per-consumer flow control on the broker.
