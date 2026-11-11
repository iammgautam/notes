---
title: Consistency, CAP and PACELC
section: hld
order: 8
summary: Consistency models from linearizable to eventual, what CAP and PACELC really say, and how quorums tune the trade-off.
---

## Consistency models

From strongest to weakest:

| Model | Guarantee | Example |
|---|---|---|
| Linearizable (strong) | Every read sees the latest committed write, as if one copy | Single Postgres primary, etcd, Spanner |
| Sequential | All nodes see operations in the same order (not necessarily real time) | Some replicated logs |
| Causal | Causally related ops seen in order; concurrent ones may differ | Reply never appears before its message |
| Read-your-writes | You always see your own writes | Profile edit visible after refresh |
| Monotonic reads | You never see time go backwards | Pinned replica per session |
| Eventual | Replicas converge if writes stop | DNS, async replicas, Cassandra at `ONE` |

Do not confuse with ACID "C" (constraints hold) or isolation levels (concurrent transactions on one DB).

## CAP

In a **network partition (P)**, a distributed system must choose:

- **CP:** refuse some requests to stay consistent (return error / timeout). etcd, ZooKeeper, HBase, a Postgres primary with sync replication.
- **AP:** keep serving, possibly stale or conflicting data. Cassandra, DynamoDB (default), DNS.

Notes:
- P is not optional across machines. The real choice is C vs A *during* a partition.
- "Availability" in CAP means every non-failed node responds. Most "CP" systems are still highly available in practice.
- CAP says nothing about normal operation. That is what PACELC adds.

## PACELC

> If **P**artition: choose **A** or **C**. **E**lse (normal): choose **L**atency or **C**onsistency.

| System | P → | E → |
|---|---|---|
| DynamoDB, Cassandra (default) | A | L |
| Spanner, CockroachDB | C | C |
| Postgres + async replicas | C (primary) | L if reading replicas |

The E/L–C choice matters daily: synchronous replication or quorum reads add latency on every request.

## Quorums

With N replicas, write to W, read from R:

- **R + W > N** → read and write sets overlap, so a read sees the latest acknowledged write (with caveats: sloppy quorums, concurrent writes, clock-based conflict resolution).
- N=3, W=2, R=2: tolerates one node down for both reads and writes.
- W=1, R=N: fast writes, slow reads. W=N, R=1: the reverse, and a single node down blocks writes.
- R + W ≤ N: faster, eventually consistent.

```text
N=3, W=2, R=2
write v2 -> [A:v2] [B:v2] [C:v1]
read     ->        [B:v2] [C:v1]  -> newest wins: v2
```

Supporting mechanisms: **read repair** (fix stale replicas on read), **hinted handoff** (another node holds writes for a down node), **anti-entropy** with Merkle trees.

## Trade-offs

| Want | Pay with |
|---|---|
| Linearizability | Latency (consensus round trips), lower availability in partitions |
| High availability, low latency | Stale reads, conflict resolution |
| Multi-region strong consistency | Cross-region RTT on writes (~100 ms) |

## Choosing per feature

| Feature | Model |
|---|---|
| Payments, inventory, unique usernames | Strong (single leader or consensus) |
| Likes, view counts | Eventual |
| Social feed | Eventual + read-your-writes for the author |
| Chat within a conversation | Causal / per-conversation ordering |
| Config, leader election, locks | Linearizable (etcd/ZooKeeper) |

## Gotchas

- "We use eventual consistency" without saying how conflicts resolve is incomplete. Name LWW, merge or CRDT.
- Last-write-wins with wall clocks loses writes under clock skew.
- Distributed locks in Redis are not safe for correctness without fencing tokens.
- Strong consistency for a whole system is rarely needed. Decide per entity.

> **Interview tip:** Pick consistency per data type and say why. "Balance is strong in Postgres; the follower count is eventually consistent and updated async."
