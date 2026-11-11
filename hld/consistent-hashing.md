---
title: Consistent hashing
section: hld
order: 10
summary: Map keys to nodes so adding or removing a node moves only about 1/N of keys, using a hash ring with virtual nodes.
---

## What it is

A way to assign keys to nodes so that changing the node count remaps only a small fraction of keys.

Naive `hash(key) % N` remaps almost every key when N changes (going from 4 to 5 nodes moves ~80% of keys). For a cache that means a near-total miss storm; for a datastore, a near-total data move.

## How it works

- Hash both nodes and keys onto the same ring (e.g. 0 to 2^32 − 1).
- A key belongs to the first node clockwise from its hash.
- Adding a node takes over only the keys between it and its predecessor. Removing a node hands its keys to its successor.
- Expected movement: ~K/N keys (K keys, N nodes).

```text
  ring positions 0..99, clockwise = increasing

            N1 (10)
           /       \
     N4 (85)       k1 (22)  -> next node clockwise is N2
        |             |
     k3 (70)       N2 (40)
           \       /
            N3 (60)

  k3 (70) -> N4.  Add N5 at 30: only keys in (10, 30] move N2 -> N5.
```

### Virtual nodes

With few physical nodes, ring segments are uneven. Each physical node gets many points on the ring (vnodes, e.g. 100–256).

- Smooths load distribution.
- On node loss, its keys spread across many nodes, not dumped on one neighbor.
- Weight bigger machines with more vnodes.

```python
import bisect, hashlib

def h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)

class Ring:
    def __init__(self, nodes, vnodes=128):
        self.points = sorted((h(f"{n}#{i}"), n) for n in nodes for i in range(vnodes))
        self.hashes = [p for p, _ in self.points]

    def node_for(self, key: str) -> str:
        i = bisect.bisect(self.hashes, h(key)) % len(self.points)
        return self.points[i][1]
```

Lookup is `O(log V)` with V total vnodes.

### Replication on the ring

Store each key on the next R distinct physical nodes clockwise (Dynamo, Cassandra). Skip vnodes that belong to a node already chosen.

## When to use

- Distributed caches (client-side sharding across Memcached nodes).
- Partitioned datastores: Cassandra, DynamoDB, Riak.
- Load balancers needing affinity (route the same user/session/key to the same backend).
- Sharding where nodes come and go.

## Alternatives

| Approach | Movement on resize | Notes |
|---|---|---|
| `hash % N` | ~all keys | Fine if N never changes |
| Consistent hash ring + vnodes | ~1/N | Classic; memory for vnodes |
| Rendezvous (HRW) hashing | ~1/N | Score every node per key, pick max; `O(N)` lookup, no ring |
| Jump consistent hash | ~1/N | Tiny and fast; nodes must be numbered 0..N−1, only add/remove at the end |
| Fixed slots (Redis Cluster: 16384) | Move slots explicitly | Simple, operator-controlled rebalancing |
| Directory / lookup table | Anything you choose | Most flexible, needs a metadata service |

## Trade-offs

| Pro | Con |
|---|---|
| Minimal data movement on resize | Still uneven without vnodes |
| No central coordinator needed for lookup | Clients must agree on ring membership |
| Natural replica placement | Hot keys still hot (hashing does not fix skew) |

## Gotchas

- Consistent hashing spreads **keys**, not **load**. A single hot key still lands on one node. Add caching or key splitting.
- Membership changes must propagate to all clients (gossip, ZooKeeper/etcd) or clients route differently.
- Use a stable hash (MD5, Murmur, xxHash), never a language's randomized `hash()` (Python salts `str` hashes per process).
- Data still needs to physically move on resize; plan for streaming it in the background.
- "Consistent hashing with bounded loads" caps each node at (1 + ε) × average, useful for LB affinity.

> **Interview tip:** Mention it when the question involves sharding a cache or datastore that scales up and down. Say "ring with virtual nodes, ~1/N keys move on resize, replicas on the next R distinct nodes".
