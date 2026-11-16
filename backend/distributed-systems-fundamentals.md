---
title: Distributed systems fundamentals
section: backend
order: 9
phase: "Phase 1 · Backend architecture"
summary: The fallacies, partial failure, partitions, latency numbers, clocks (NTP, Lamport, vector), failure models, idempotence, quorums and the end-to-end argument.
---

The moment a backend has two processes that talk over a network (a Django app and PostgreSQL, a web pod and a Celery worker, two services), it is a distributed system. That brings failure modes a single process never has: a call that neither succeeded nor failed, two machines that disagree on what time it is, and a database that is up but unreachable from half your servers. Every pattern later in this section (retries, idempotency keys, consensus, sagas, quorums) exists to cope with the handful of facts in this note.

## Distributed system fundamentals

### The fallacies of distributed computing

Attributed to L Peter Deutsch and colleagues at Sun in the 1990s, with the eighth added by James Gosling. Each assumption holds on a laptop and fails in production.

| Fallacy | Reality | Design consequence |
|---|---|---|
| The network is reliable | Packets drop, connections reset, switches fail | Timeouts on every call, retries, idempotency |
| Latency is zero | A same-zone round trip is ~0.1-0.5 ms, cross-region tens to hundreds of ms | Batch, avoid chatty calls, fan out in parallel |
| Bandwidth is infinite | Large payloads saturate links and NICs | Paginate, compress, stream |
| The network is secure | Anyone on the path can read or alter traffic | TLS everywhere, mTLS internally, zero trust |
| Topology doesn't change | Pods and IPs churn constantly | Service discovery, no hard-coded IPs |
| There is one administrator | Many teams, providers and policies | Defensive configs, clear ownership, contracts |
| Transport cost is zero | Serialization CPU and cross-zone egress fees | Efficient formats, zone-aware routing |
| The network is homogeneous | Mixed protocols, MTUs, hardware | Standard protocols, explicit versioning |

### Partial failure

In one process, a function call either returns or the whole process crashes. Across a network, a call has three outcomes: **success**, **failure**, or **unknown**. A timeout is "unknown": the request may have been lost before arriving, processed and the response lost, or still be running.

```text
 client                  network                 payment service
   | ---- charge $50 ----->                             |
   |                       ------ charge $50 ---------> | debits card
   |                       <----- 200 OK -------- X     | (reply lost)
   | (timeout after 2 s)                                |
   |   did the charge happen?  unknowable from here     |
   | ---- retry: charge $50 ------------------------->  | debits AGAIN
```

Consequences you design for:

- **Every remote call needs a timeout.** Without one, a hung dependency hangs your thread or connection forever.
- **Retrying is only safe for idempotent operations**, which is why idempotency keys exist; see [Idempotency]({{ '/backend/idempotency/' | relative_url }}).
- **Gray failure**: a node that is slow or passes health checks but fails real traffic is often worse than a dead one, because failure detectors miss it.
- **Process pauses look like failures.** A stop-the-world GC pause, VM migration or swapped-out page can freeze a process for seconds. It then wakes up believing it still holds a lease or a lock that expired. Fencing tokens (a monotonically increasing number checked by the storage) protect against the stale holder; see [Distributed locking]({{ '/backend/distributed-locking/' | relative_url }}).

### Network partitions

A partition is when some nodes can't communicate with others while each side keeps running. Partitions are not rare events from textbooks; GitHub publicly described an October 2018 incident where 43 seconds of lost connectivity between data centers triggered a database failover that left the site degraded for about a day.

- **Symmetric vs asymmetric**: A can reach B but B can't reach A; or a node can talk to clients but not to its peers.
- **Split brain**: both sides elect a leader and accept writes, producing conflicting histories. Prevented by requiring a **majority quorum** for leadership, so at most one side can win; see [Leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}).
- During a partition you choose between rejecting requests (consistency) and serving possibly stale or conflicting data (availability); that is the CAP trade-off, covered in [CAP and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}).

### Latency numbers

Rounded orders of magnitude, in the spirit of the "latency numbers every programmer should know" list associated with Jeff Dean and Peter Norvig. Hardware and clouds vary; measure your own.

| Operation | Rough time |
|---|---|
| L1 cache reference | ~1 ns |
| Main memory reference | ~100 ns |
| Read 1 MB sequentially from memory | tens of µs |
| Random 4 KB read, NVMe SSD | ~10-100 µs |
| Round trip, same availability zone | ~0.1-0.5 ms |
| Redis GET or simple indexed SQL query, incl. network | ~0.2-2 ms |
| Round trip, across zones in one region | ~0.5-2 ms |
| Disk seek, spinning HDD | ~5-10 ms |
| Round trip, US east to US west | ~60-80 ms |
| Round trip, US to Europe | ~80-120 ms |

What to take from it: memory is about a thousand times faster than a network hop; a network hop inside a zone is cheap relative to a cross-region one; and a page that makes 30 sequential queries at 1 ms each spends 30 ms on round trips alone. That is the N+1 problem in one line; see [Query optimization and N+1]({{ '/backend/query-optimization-and-n-plus-one/' | relative_url }}).

## Time and clocks

Ordering events is central to correctness (which write wins, did the lock expire, which message came first), and distributed systems have no shared clock.

### Physical clocks and NTP skew

Each machine has a quartz oscillator that drifts with temperature and age. Google's Spanner paper budgets for a worst-case drift of 200 µs per second, which is 17 seconds per day if never corrected. NTP corrects clocks against reference servers; over the public internet expect accuracy of a few to tens of milliseconds, better on a good LAN, and much worse under network congestion or misconfiguration. Cloud providers' local time services can do much better on supported instances.

Problems that follow:

- **Skew between machines**: two nodes disagree by milliseconds or more. "Last write wins" by wall-clock timestamp silently drops the write with the lower timestamp even if it happened later.
- **Clocks jump**: NTP may step a clock backward or forward. Leap seconds historically caused outages; large providers now smear them over hours.
- **Wall clock vs monotonic clock**: `time.time()` can jump; `time.monotonic()` only moves forward and is the one to use for timeouts, durations and rate limiters. Monotonic values are meaningless across machines.

Spanner (publicly described) takes a different approach: TrueTime exposes clock uncertainty as an interval `[earliest, latest]` from GPS and atomic clocks, and a transaction waits out the uncertainty (commit wait, typically a few milliseconds) before its timestamp is visible, giving externally consistent ordering. CockroachDB and others use **hybrid logical clocks** (HLC), which combine physical time with a logical counter: timestamps stay close to wall time but still respect causality.

### Lamport clocks

Leslie Lamport's 1978 paper defined **happened-before** (`a → b`): `a` precedes `b` in the same process, or `a` is the send and `b` the receive of the same message, or transitively through other events. Events not ordered either way are **concurrent**.

A Lamport clock is one counter per process:

1. Before each local event or send, increment the counter.
2. Attach the counter to every message.
3. On receive, set `counter = max(counter, message_counter) + 1`.

Guarantee: if `a → b` then `L(a) < L(b)`. The converse does **not** hold: `L(a) < L(b)` doesn't mean `a` caused `b`. Breaking ties with the process ID gives a **total order** consistent with causality, which is enough for things like a fair distributed mutex queue or deterministic ordering of log entries.

### Vector clocks

To detect concurrency, each process keeps a vector with one counter per process:

1. On a local event or send, increment your own entry.
2. Attach the whole vector to messages.
3. On receive, take the element-wise max with the incoming vector, then increment your own entry.

Compare `V(a) ≤ V(b)` element-wise: if all entries are ≤ and the vectors differ, `a → b`. If neither dominates, the events are **concurrent**, which in a replicated store means a genuine conflict that needs merging or surfacing to the client. Amazon's Dynamo paper described using vector clocks this way, returning conflicting versions to the application to reconcile (a shopping cart merges by union). The cost is size: one entry per participant, so systems prune or use variants such as dotted version vectors that track replicas instead of clients.

| | Physical timestamp | Lamport | Vector | HLC |
|---|---|---|---|---|
| Size | 1 number | 1 number | N numbers | 1 number + counter |
| Respects causality | No (skew) | Yes, one way | Yes, exactly | Yes, one way |
| Detects concurrency | No | No | Yes | No |
| Close to real time | Yes | No | No | Yes |

## Failure models

What kinds of failure an algorithm tolerates determines how complex it must be.

| Model | What a faulty node does | Example handling |
|---|---|---|
| Crash-stop | Halts forever | Replace it, fail over |
| Crash-recovery | Halts, restarts later with durable state only | Write-ahead logs, replay |
| Omission | Drops some messages | Retries, acknowledgments |
| Timing (performance) | Responds, but too slowly | Timeouts, hedged requests |
| Byzantine | Anything, including lying | BFT protocols; rare inside one company |

Timing assumptions matter as much. In a **synchronous** model, message delay has a known bound; in an **asynchronous** model it is unbounded, and the FLP result (Fischer, Lynch and Paterson, 1985) shows deterministic consensus can't be guaranteed if even one process may crash. Real systems assume **partial synchrony**: the network behaves within bounds most of the time, so Raft and Paxos stay safe always and make progress when the network is calm. Most backend infrastructure assumes crash-recovery nodes and partially synchronous networks, and treats Byzantine faults as a security problem (authentication, checksums) rather than a protocol one.

Failure detection is always a guess: a timeout can't distinguish a dead node from a slow one. Short timeouts cause false failovers; long ones slow recovery. Adaptive detectors such as the phi accrual detector (used by Cassandra and Akka) output a suspicion level based on observed heartbeat intervals instead of a fixed cutoff.

## Idempotence

An operation is idempotent if applying it more than once has the same effect as applying it once. Because of partial failure, any operation might be retried, so non-idempotent operations either get duplicated or must never be retried.

| Naturally idempotent | Not idempotent | Make it idempotent with |
|---|---|---|
| `PUT /users/7 {name}` (set) | `POST /charges` | Client-generated idempotency key, stored with the result |
| `DELETE /sessions/abc` | `balance = balance + 10` | Operation ID in a unique-constraint dedupe table |
| `UPDATE ... SET status='paid' WHERE id=9` | Append to a log or list | Conditional write: `WHERE version = 4` |

The practical formula for messaging: **at-least-once delivery plus idempotent consumers gives effectively-once processing**; see [Delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}).

## Quorums

With `N` replicas, a write waits for `W` acknowledgments and a read queries `R` replicas.

- If `R + W > N`, every read set overlaps every write set in at least one replica, so a read sees the latest acknowledged write (picking the newest version among responses).
- If `W > N/2`, two conflicting writes can't both reach a quorum without overlapping.
- Typical: `N=3, W=2, R=2` tolerates one replica down for both reads and writes.
- Majority quorums are the core of Raft and Paxos: with 5 nodes, any 3 form a majority and the cluster tolerates 2 failures. Use odd cluster sizes; a 4-node cluster tolerates only 1 failure, the same as 3.

Quorums alone don't make a system linearizable. Dynamo-style stores with **sloppy quorums** (writes accepted by any reachable nodes, handed off later) break the overlap guarantee; a write that fails after reaching some replicas may or may not appear in later reads; concurrent reads during a write can return old and new values in either order unless readers write back what they read. Replicas still need anti-entropy and read repair to converge. See [Replication]({{ '/backend/replication/' | relative_url }}).

## The end-to-end argument

Saltzer, Reed and Clark (1984): a function that can only be implemented correctly with knowledge held by the application at the endpoints should be implemented there; lower layers can provide it only as a performance optimization. Their example: TCP checksums each packet, but a file can still be corrupted by a disk, a buggy buffer copy or a crash mid-transfer, so a careful file transfer checks a checksum of the whole file end to end.

Backend applications of the same idea:

- **Duplicate suppression**: TCP removes duplicate packets within one connection, but a client that times out and retries on a new connection sends a new request. Only an application-level idempotency key, checked by the service that performs the side effect, prevents a double charge.
- **Exactly-once in a broker** (for example Kafka transactions) covers reads and writes within Kafka, not the email you send or the row you insert in PostgreSQL. The end-to-end guarantee needs idempotent side effects.

## In practice

### Lamport and vector clocks, plus quorum overlap

A runnable stdlib demo: three nodes, one message from A to B, while C works alone. Lamport timestamps order the causal pair but can't tell that C's events are concurrent; vector clocks can. The last part brute-forces quorum overlap.

```python
from itertools import combinations


class LamportClock:
    def __init__(self):
        self.time = 0

    def local(self) -> int:              # also used for send
        self.time += 1
        return self.time

    def receive(self, msg_time: int) -> int:
        self.time = max(self.time, msg_time) + 1
        return self.time


class VectorClock:
    def __init__(self, node: int, n: int):
        self.node, self.clock = node, [0] * n

    def local(self) -> tuple:            # also used for send
        self.clock[self.node] += 1
        return tuple(self.clock)

    def receive(self, msg_clock: tuple) -> tuple:
        self.clock = [max(a, b) for a, b in zip(self.clock, msg_clock)]
        return self.local()


def happened_before(a: tuple, b: tuple) -> bool:
    return all(x <= y for x, y in zip(a, b)) and a != b


def concurrent(a: tuple, b: tuple) -> bool:
    return not happened_before(a, b) and not happened_before(b, a)


def quorums_always_overlap(n: int, r: int, w: int) -> bool:
    nodes = range(n)
    return all(set(rq) & set(wq)
               for rq in combinations(nodes, r) for wq in combinations(nodes, w))


if __name__ == "__main__":
    # Three nodes. A writes and sends to B. C works alone the whole time.
    la, lb, lc = LamportClock(), LamportClock(), LamportClock()
    va, vb, vc = VectorClock(0, 3), VectorClock(1, 3), VectorClock(2, 3)

    c1_l, c1_v = lc.local(), vc.local()          # C: event c1
    c2_l, c2_v = lc.local(), vc.local()          # C: event c2
    c3_l, c3_v = lc.local(), vc.local()          # C: event c3
    a1_l, a1_v = la.local(), va.local()          # A: send m1
    b1_l, b1_v = lb.receive(a1_l), vb.receive(a1_v)   # B: receive m1

    # Lamport: causality implies a smaller timestamp...
    assert a1_l < b1_l
    assert (a1_l, b1_l) == (1, 2)
    # ...but not the reverse: c3 has a larger timestamp yet did not cause b1.
    assert c3_l > b1_l

    # Vector clocks capture causality exactly.
    assert a1_v == (1, 0, 0) and b1_v == (1, 1, 0) and c3_v == (0, 0, 3)
    assert happened_before(a1_v, b1_v)
    assert concurrent(c3_v, b1_v) and concurrent(c1_v, a1_v)
    assert happened_before(c1_v, c3_v)           # same node, program order

    # Total order for tie-breaks (e.g. a mutex queue): (lamport time, node id).
    events = [(b1_l, 1, "b1"), (a1_l, 0, "a1"), (c1_l, 2, "c1")]
    assert [e[2] for e in sorted(events)] == ["a1", "c1", "b1"]

    # Quorums: R + W > N is exactly what guarantees overlap.
    assert quorums_always_overlap(3, 2, 2)
    assert not quorums_always_overlap(3, 1, 2)
    assert quorums_always_overlap(5, 2, 4)
    assert not quorums_always_overlap(5, 2, 3)
    print("clocks demo ok")
```

### Timeouts and clocks in everyday Python

Illustrative patterns that apply these ideas in a Django or worker codebase:

```python
import time
import requests

# Every remote call gets a (connect, read) timeout. The library default is none.
resp = requests.post(
    "https://payments.internal/charges",
    json={"order_id": 981, "amount_cents": 5000},
    headers={"Idempotency-Key": "order-981-charge"},   # makes the retry safe
    timeout=(0.5, 3),
)

# Durations and deadlines use the monotonic clock, never time.time().
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    ...
```

## Pitfalls

- **No timeouts.** Python `requests` and many clients default to waiting forever. Symptom: worker threads all stuck, CPU idle, queue growing. Set connect and read timeouts on every client, including database and Redis.
- **Treating a timeout as failure.** Marking an order "payment failed" after a timeout and letting the user pay again double-charges. Treat unknown as unknown: reconcile by querying the downstream with the idempotency key.
- **Last write wins by wall clock.** Two app servers 50 ms apart in clock time silently discard updates. Use database-assigned versions, logical clocks, or conditional writes.
- **Wall clock for durations.** A rate limiter or lease built on `time.time()` misbehaves when NTP steps the clock. Use `time.monotonic()`.
- **Leases without fencing.** A paused process resumes and writes after its lock expired. Pass a fencing token to storage and reject stale ones.
- **Even-sized clusters and two data centers.** A 2-plus-2 split can't form a majority on either side. Use odd sizes and a third location (even a lightweight tie-breaker) for quorum.

## Interview questions

**A request to another service times out. Did it succeed?** Unknown. It may not have arrived, may have completed with the reply lost, or may still be running. Retry only if the operation is idempotent (or carries an idempotency key), and otherwise reconcile by querying state.

**Why can't you order events across servers by timestamp?** Clocks drift and are corrected imperfectly, so timestamps from different machines can disagree by milliseconds or more and even go backward. Use a single source of ordering (a database sequence, a log offset, a leader), logical clocks, or bounded-uncertainty clocks like TrueTime with commit wait.

**Lamport vs vector clocks?** Both respect causality. Lamport clocks are one integer and give a total order consistent with causality but can't detect concurrency. Vector clocks keep one counter per node and can tell whether two events are causally related or concurrent, at a cost that grows with the number of nodes.

**What does R + W > N give you, and what doesn't it?** Overlap between every read and write quorum, so a read contacts at least one replica with the latest acknowledged write. It doesn't by itself give linearizability: sloppy quorums, partially failed writes and concurrent reads can still return stale or flip-flopping values without read repair and write-back.

**Why do consensus clusters have 3 or 5 nodes?** Majority quorums: 3 nodes tolerate 1 failure, 5 tolerate 2. Even sizes add cost without tolerating more failures (4 tolerates 1) and risk ties on a split.

**What is the end-to-end argument, applied to a payment API?** Lower layers (TCP, retries in a load balancer, broker exactly-once) can't guarantee no double charge, because duplicates can come from client retries over new connections. Only the payment service, deduplicating on an idempotency key it stores atomically with the charge, can provide the guarantee.

## Further reading

- Leslie Lamport, "Time, Clocks, and the Ordering of Events in a Distributed System" (Communications of the ACM, 1978).
- J. H. Saltzer, D. P. Reed and D. D. Clark, "End-to-End Arguments in System Design" (ACM Transactions on Computer Systems, 1984).
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly, 1st edition), chapter 8, "The Trouble with Distributed Systems", and chapter 5 on quorums.
- Giuseppe DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store" (SOSP 2007).
- James C. Corbett et al., "Spanner: Google's Globally-Distributed Database" (OSDI 2012).
