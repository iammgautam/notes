---
title: CAP, consistency and PACELC
section: backend
order: 35
phase: "Phase 5 · Distributed systems"
summary: What CAP really says, the consistency models ladder, linearizability vs serializability, quorums, eventual consistency, CRDTs and PACELC.
---

Once data lives on more than one machine, a read can miss the latest write. A user renames their profile, refreshes, and sees the old name from a lagging replica. Two data centers lose contact and both sell the last item in stock. CAP, the consistency models and PACELC are the vocabulary for deciding which of these behaviors you accept, where, and at what cost in latency and availability.

## CAP theorem

### What it actually says

Brewer stated it as a conjecture in 2000; Gilbert and Lynch proved it in 2002 with precise definitions:

- **Consistency (C)** means linearizability: every read returns the value of the most recent completed write, as if there were a single copy of the data.
- **Availability (A)** means every request received by a non-failing node eventually gets a non-error response. Not "99.99% uptime"; every node, every request.
- **Partition tolerance (P)** means the system keeps its guarantees even when the network drops arbitrary messages between nodes.

The theorem: when a network partition happens, a system must choose between C and A. The proof is one picture. Two replicas lose contact; a client writes `x = 2` to one, another client reads `x` from the other. The second replica either answers with the old value (gives up C) or waits or errors until the partition heals (gives up A).

```text
   client 1                               client 2
      | write x=2                             | read x
      v                                       v
  +--------+     network partition       +--------+
  | node A | ----------XXXX------------- | node B |
  |  x = 2 |                             |  x = 1 |
  +--------+                             +--------+
                    node B must choose:
         answer x=1 (available, not consistent)
      or refuse/wait until healed (consistent, not available)
```

### CAP when choosing a database

"Pick two of three" is the wrong reading. Partitions are not optional in a system that spans machines, so "CA" only describes a single-node database or a system that simply breaks during partitions. The real choice is what each operation does *during* a partition:

| Behavior during a partition | Label | Examples (typical configuration) |
|---|---|---|
| Minority side refuses writes and linearizable reads | CP | etcd, ZooKeeper, Consul, Spanner, Postgres with synchronous standby required |
| Every reachable replica keeps accepting reads and writes; reconcile later | AP | Cassandra and Riak at low consistency levels, DynamoDB eventually consistent reads, DNS |
| Neither guarantee in the strict sense | Neither | Async-replicated primary with failover (Postgres, MySQL, Redis): a failover can lose acknowledged writes, and replica reads are stale |

Two consequences matter in interviews. A "CP" system can still be highly available in practice: etcd on five nodes keeps serving while any three can talk; CAP availability is a far stronger, every-node property. And most real databases are tunable or neither, which is why Kleppmann argues against labeling them CP or AP at all: describe what a specific operation does under a specific failure. Brewer later described Spanner as technically CP but "effectively CA", because Google's network makes partitions rare enough that the unavailability is negligible.

### CAP for distributed services

CAP applies to your own services too, not just databases. A rate limiter with a central Redis, an inventory service across two regions, a feature-flag service: each must decide what to do when it cannot reach the source of truth.

- **Fail closed (choose C):** payments, inventory decrements, unique username claims, leader leases. A wrong answer costs money or correctness.
- **Fail open or serve stale (choose A):** feeds, recommendations, view counters, feature flags with a cached last-known value, rate limiters (usually). A slightly wrong answer is better than an error page.

Most systems make this choice per operation, not per system: an e-commerce site serves the catalog from stale caches during an outage but refuses checkout if it cannot reserve stock.

### What CAP does not say

- Nothing about latency, the trade-off you make every day, not just during partitions (see PACELC).
- Nothing about messier failures: slow nodes, one-way message loss, GC pauses.
- Nothing about transactions or multi-object invariants. C in CAP is single-register linearizability, unrelated to C in ACID.

## Consistency models ladder

Between "linearizable" and "eventually the same" sits a ladder of models. Stronger models are easier to program against and cost more coordination, latency and availability.

```text
 stronger
   strict serializability   serializable + respects real-time order
        |             \
   linearizability     serializability  (transactions, any serial order)
        |
   sequential           one global order, consistent with each client
        |
   causal               cause before effect; concurrent ops unordered
        |
   session guarantees   read-your-writes, monotonic reads,
        |               monotonic writes, writes-follow-reads
   eventual             replicas converge once writes stop
 weaker
```

| Model | Guarantee | Typical source |
|---|---|---|
| Linearizable | Single up-to-date copy, real-time order | Consensus stores, reads from a leader with a lease |
| Sequential | Everyone sees the same order, maybe lagging | Some replicated logs |
| Causal | A reply never appears before the message it answers | Causal+ stores, COPS-style designs, MongoDB causal sessions |
| Read-your-writes | You see your own writes | Sticky routing, LSN tracking |
| Monotonic reads | You never see data go back in time | Pin a session to one replica |
| Eventual | Converges eventually; anything in between | Async replicas, DNS, caches |

Causal consistency is the strongest model that stays available during partitions. The session guarantees (Terry et al., 1994) are the cheap middle: most user-facing "consistency bugs" violate read-your-writes or monotonic reads, and both are fixable in the routing layer without a stronger database.

## Strong consistency

### Linearizability vs serializability

These are constantly confused.

| | Linearizability | Serializability |
|---|---|---|
| Scope | Single operations on single objects | Transactions over many objects |
| Promise | Each operation takes effect atomically at some instant between its start and end; real-time order is respected | The outcome equals *some* serial execution of the transactions |
| Real time | Yes: once a write completes, every later read sees it | No: the serial order can differ from wall-clock order |
| Typical violation | Reading an old value after a write was acknowledged | Write skew under snapshot isolation |
| Provided by | Raft/Paxos stores, a single leader serving reads | Two-phase locking, SSI (Postgres `SERIALIZABLE`) |

Strict serializability is both at once: transactions appear in a serial order that matches real time. Spanner provides it using TrueTime. Isolation levels on a single node are covered in [ACID and transactions]({{ '/backend/acid-and-transactions/' | relative_url }}) and [transactions and isolation]({{ '/postgresql/transactions-and-isolation/' | relative_url }}).

### Quorums: R + W > N

Leaderless (Dynamo-style) stores replicate each key to `N` nodes. A write succeeds after `W` acknowledgments; a read queries `R` replicas and takes the newest version. If `R + W > N`, every read set overlaps every successful write set in at least one replica, so the newest acknowledged write is always among the responses.

| N, W, R | Property |
|---|---|
| 3, 2, 2 | Common default: tolerates one slow or dead replica for both reads and writes |
| 3, 3, 1 | Fast reads, but any one dead replica blocks writes |
| 3, 1, 3 | Fast writes, but reads need every replica |
| 3, 1, 1 | Fastest, eventual consistency only |

Overlap is necessary but not sufficient for linearizability. Quorums can still misbehave:

- **Concurrent writes** are ordered by timestamp (last write wins), so clock skew silently discards writes.
- **A read concurrent with a write** can see the new value from one replica, then a later read sees the old value from others, unless reads write back the value they return before answering (the ABD algorithm).
- **Failed writes are not rolled back.** A write that reached fewer than `W` replicas reports failure but stays on the replicas it reached and may surface later.
- **Sloppy quorums** (writes accepted by stand-in nodes during failures, with hinted handoff) break the overlap guarantee entirely.

Systems that need linearizability use a single leader per shard replicated with consensus (Raft, Paxos, Zab), as covered in [leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}).

## Eventual consistency

### Convergence

Eventual consistency promises only that if writes stop, all replicas eventually return the same value; it says nothing about how long that takes or what reads return meanwhile. Convergence needs every update to reach every replica, and replicas that applied the same updates in different orders to end in the same state. The second part is conflict resolution:

| Strategy | How | Cost |
|---|---|---|
| Last write wins (LWW) | Keep the highest timestamp | Silently drops concurrent writes; clock skew picks the wrong winner |
| Version vectors + siblings | Detect concurrent writes, keep both, let the app merge | Application complexity (Dynamo's shopping cart merged carts) |
| Application merge | Domain-specific rules | Must be written and tested for each type |
| CRDTs | Data types whose merge always converges | Limited operations, metadata overhead |

### Anti-entropy

A background process compares replicas and copies missing or newer data. Comparing every key is too expensive, so replicas build Merkle trees over key ranges: compare root hashes, descend only into subtrees that differ, and transfer only the differing keys. Dynamo described this; Cassandra exposes it as `nodetool repair`, which operators must run regularly or deleted data can reappear after tombstones expire.

### Read repair

When a read queries several replicas and they disagree, the coordinator returns the newest value and writes it back to the stale replicas. It is cheap and fixes hot data quickly, but only repairs keys that are actually read; cold data still needs anti-entropy. Hinted handoff is the third mechanism: when a replica is down, another node holds the write with a "hint" and delivers it when the replica returns.

### CRDTs

Conflict-free replicated data types (Shapiro et al., 2011) are designed so that concurrent updates always merge to the same result, with no coordination. State-based CRDTs need a merge function that is commutative, associative and idempotent, so replicas can exchange state in any order, any number of times.

- **G-Counter:** one counter per replica; increment your own slot; merge takes the per-slot max; value is the sum.
- **PN-Counter:** two G-Counters, one for increments and one for decrements.
- **OR-Set:** adds are tagged with unique IDs; a remove only removes the tags it has seen, so a concurrent add wins.
- **LWW-Register, MV-Register:** single values resolved by timestamp or kept as concurrent siblings.
- **Sequence CRDTs:** collaborative text editing (Automerge, Yjs).

Riak's data types, Redis Enterprise Active-Active and collaborative editors are publicly described as CRDT-based. The limit: CRDTs cannot enforce invariants that need agreement, such as "stock never goes below zero" or "username is unique". Those operations need coordination.

## PACELC

Abadi (2012) extended CAP to cover normal operation: if there is a **P**artition, choose **A** or **C**; **E**lse, choose **L**atency or **C**onsistency. Strong consistency costs a quorum or leader round trip on every operation, even when nothing is broken; across regions that is tens of milliseconds per write, every day.

| System (typical configuration) | If partition | Else |
|---|---|---|
| Cassandra, Riak, Dynamo | PA | EL |
| etcd, ZooKeeper, Spanner | PC | EC |
| MongoDB (as classified by Abadi in 2012) | PA | EC |
| Yahoo PNUTS | PC | EL |

PACELC is more useful than CAP in design reviews because the "else" branch is where you live 99.9% of the time. Many stores let you choose per request: Cassandra consistency levels, DynamoDB `ConsistentRead=True` (at twice the read cost), Azure Cosmos DB's five levels (strong, bounded staleness, session, consistent prefix, eventual).

## In practice

A runnable quorum store showing that `R + W > N` eliminates stale reads, that quorums refuse writes on the minority side, that read repair and anti-entropy converge replicas, and that a CRDT counter converges in any merge order:

```python
import random


class Replica:
    def __init__(self, name):
        self.name, self.version, self.value, self.up = name, 0, None, True


class QuorumStore:
    """N replicas; a write is acked after W replicas store it, a read polls R.

    Versions come from one counter here. Real systems use per-key timestamps,
    vector clocks or a leader-assigned sequence number.
    """

    def __init__(self, n=3, w=2, r=2, seed=1):
        self.replicas = [Replica(f"r{i}") for i in range(n)]
        self.w, self.r, self.clock = w, r, 0
        self.rng = random.Random(seed)

    def _live(self, need):
        live = [x for x in self.replicas if x.up]
        if len(live) < need:
            raise RuntimeError("quorum unavailable")  # the CP choice: refuse
        return live

    def write(self, value):
        self.clock += 1
        # Acked once W replicas have it; the others have not seen it yet.
        for x in self.rng.sample(self._live(self.w), self.w):
            x.version, x.value = self.clock, value
        return self.clock

    def read(self, repair=True):
        polled = self.rng.sample(self._live(self.r), self.r)
        newest = max(polled, key=lambda x: x.version)
        if repair:  # read repair: fix stale replicas we happened to poll
            for x in polled:
                x.version, x.value = newest.version, newest.value
        return newest.version, newest.value

    def anti_entropy(self):
        """Background sync (Merkle-tree comparison in real systems)."""
        newest = max(self.replicas, key=lambda x: x.version)
        for x in self.replicas:
            if x.up:
                x.version, x.value = newest.version, newest.value

    def diverged(self):
        return len({x.version for x in self.replicas}) > 1


def stale_read_rate(n, w, r, trials=3000):
    stale = 0
    for t in range(trials):
        s = QuorumStore(n, w, r, seed=t)
        s.write("v1")
        s.anti_entropy()
        latest = s.write("v2")
        version, _ = s.read(repair=False)
        stale += version != latest
    return stale / trials


class GCounter:
    """Grow-only counter CRDT: one slot per replica, merge = element-wise max."""

    def __init__(self, rid):
        self.rid, self.counts = rid, {}

    def incr(self, n=1):
        self.counts[self.rid] = self.counts.get(self.rid, 0) + n

    def merge(self, other):
        for rid, c in other.counts.items():
            self.counts[rid] = max(self.counts.get(rid, 0), c)

    def value(self):
        return sum(self.counts.values())


if __name__ == "__main__":
    # 1. R + W > N: every read set overlaps the latest write set.
    for n, w, r in [(3, 1, 1), (3, 1, 2), (3, 2, 2), (3, 3, 1), (5, 3, 3)]:
        rate = stale_read_rate(n, w, r)
        print(f"N={n} W={w} R={r}  R+W>N={r + w > n!s:5}  stale reads {rate:.2f}")
        assert (rate == 0) == (r + w > n)

    # 2. Quorums tolerate N - W failed replicas for writes, then refuse.
    s = QuorumStore(3, 2, 2)
    s.replicas[2].up = False
    s.write("still ok")                      # 2 of 3 is a quorum
    s.replicas[1].up = False
    try:
        s.write("minority side")
        raise AssertionError("write should have been refused")
    except RuntimeError:
        pass                                 # consistent, not available

    # 3. Read repair and anti-entropy converge lagging replicas.
    s = QuorumStore(3, 2, 2, seed=5)
    s.write("a")
    s.anti_entropy()
    s.write("b")
    assert s.diverged()
    for _ in range(20):
        assert s.read() == (2, "b")
    s.anti_entropy()
    assert not s.diverged()

    # 4. CRDT counter: concurrent increments, any merge order, same result.
    a, b, c = GCounter("a"), GCounter("b"), GCounter("c")
    a.incr(3)
    b.incr(2)
    c.incr(5)
    a.merge(b)
    a.merge(c)
    c.merge(a)
    b.merge(c)
    b.merge(c)                               # merge is idempotent
    assert a.value() == b.value() == c.value() == 10
    print("ok")
```

Output: stale-read rates of 0.67 and 0.34 when `R + W <= N`, and 0.00 otherwise.

Quorum commit on Postgres: the primary waits for one of two standbys to apply each commit (illustrative). This is PC/EC behavior: if neither standby responds, commits block.

```sql
ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (replica_a, replica_b)';
ALTER SYSTEM SET synchronous_commit = 'remote_apply';  -- visible on standby
SELECT pg_reload_conf();

-- Per transaction, opt low-value writes out of the wait:
BEGIN;
SET LOCAL synchronous_commit = 'local';
INSERT INTO page_views (tenant_id, path) VALUES (42, '/pricing');
COMMIT;
```

Read-your-writes in Django over async replicas: after a user writes, pin their reads to the primary for a few seconds (illustrative):

```python
# routers.py
import contextvars
import time

pin_until = contextvars.ContextVar("pin_until", default=0.0)


class PrimaryReplicaRouter:
    def db_for_read(self, model, **hints):
        return "default" if time.time() < pin_until.get() else "replica"

    def db_for_write(self, model, **hints):
        return "default"


class PinAfterWriteMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        pin_until.set(float(request.COOKIES.get("pin_primary", 0)))
        response = self.get_response(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            response.set_cookie("pin_primary", str(time.time() + 5), max_age=5)
        return response
```

A time window is a heuristic: replica lag can exceed it. The precise version stores the primary's WAL position after the write (`pg_current_wal_lsn()`) and reads from a replica only once its `pg_last_wal_replay_lsn()` has passed it. Replication mechanics are in [replication]({{ '/backend/replication/' | relative_url }}).

## Pitfalls

- **"It's a CP database, so reads are fresh."** Not if you read from followers. MongoDB secondary reads, Postgres replicas and etcd serializable reads (`--consistency=s`) can all be stale. Know which node answers.
- **LWW with skewed clocks.** A node with a clock two seconds fast wins every conflict, and other writes vanish without errors. Use version vectors, server-assigned versions, or CRDTs for data that matters.
- **Treating quorums as transactions.** `R + W > N` gives no atomicity across keys and no protection against concurrent read-modify-write. Use conditional writes (compare-and-set, lightweight transactions) for those.
- **Data going back in time.** The balancer alternates between replicas with different lag, so a list shows an item, then not, then again. Pin sessions to one replica or track a minimum LSN per session.
- **Eventual consistency for invariants.** Two regions each sell the last seat. Route invariant-bearing operations (stock, balances, uniqueness) through a single leader or a consensus-backed store.
- **Ignoring the E in PACELC.** Synchronous cross-region writes add 60–150 ms to every request. Reserve them for operations that need it.

## Interview questions

**What does CAP say, precisely?** In an asynchronous network that can partition, a replicated register cannot be both linearizable and available on every non-failing node. During a partition you choose, per operation, between refusing or delaying requests (C) and answering with possibly stale data (A). Without partitions, CAP says nothing.

**Is PostgreSQL CP or AP?** Neither label fits cleanly. A single node is not distributed. With async replicas, replica reads are stale and a failover can lose acknowledged writes. With synchronous replication required, commits block when standbys are unreachable, which is CP-like behavior. Describe the configuration, not the product.

**Linearizability vs serializability?** Linearizability is about single-object operations respecting real time. Serializability is about transactions being equivalent to some serial order, not necessarily the real-time one. Strict serializability is both.

**What does R + W > N guarantee?** That every read quorum intersects every successful write quorum, so the newest acknowledged write is among the responses. It does not guarantee linearizability (concurrent reads and writes, LWW conflicts, failed partial writes, sloppy quorums) or multi-key atomicity.

**How do you give users read-your-writes with async replicas?** Route the user's reads to the primary for a short window after a write, or record the primary's log position at write time and only read from replicas that have replayed past it. Monotonic reads come from pinning a session to one replica.

**How does an eventually consistent store converge?** Updates propagate through replication, hinted handoff for down replicas, read repair on reads, and periodic anti-entropy with Merkle trees. Conflicts are resolved by LWW, version vectors with application merge, or CRDT merge functions.

**What is a CRDT and where does it fall short?** A data type whose replicas can be updated independently and merged in any order to the same state, because the merge is commutative, associative and idempotent (a G-Counter merges by per-replica max). It cannot enforce global invariants such as non-negative balances; those need coordination.

**What does PACELC add?** The latency-versus-consistency trade-off during normal operation. Strong consistency costs a quorum or leader round trip on every operation, not only during partitions.

## Further reading

- *Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services*, Seth Gilbert and Nancy Lynch (SIGACT News, 2002).
- *CAP Twelve Years Later: How the "Rules" Have Changed*, Eric Brewer (IEEE Computer, 2012).
- *Consistency Tradeoffs in Modern Distributed Database System Design*, Daniel Abadi (IEEE Computer, 2012).
- *Conflict-free Replicated Data Types*, Shapiro, Preguiça, Baquero and Zawirski (SSS 2011).
- *Designing Data-Intensive Applications*, Martin Kleppmann, chapters 5 and 9.
