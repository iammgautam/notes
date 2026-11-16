---
title: Consistent hashing
section: backend
order: 34
phase: "Phase 5 · Distributed systems"
summary: Hash rings, virtual nodes, rebalancing cost, rendezvous and jump hashing, and bounded loads, with a runnable ring that measures key movement.
---

You have ten cache nodes and route each key with `hash(key) % 10`. Add an eleventh node and about 91% of keys now map somewhere else: the cache hit rate collapses and the database takes the full read load at the worst possible moment. Consistent hashing is the family of schemes that keep most keys where they were when nodes join or leave. It shows up in cache clusters (see [distributed caching and CDN]({{ '/backend/distributed-caching-and-cdn/' | relative_url }})), Dynamo-style databases, CDNs, load balancers that need affinity, and any sharded system whose membership changes.

## Why modulo hashing fails

With `N` nodes, `node = hash(key) % N`. Going from `N` to `N + 1` keeps a key in place only when `hash % N == hash % (N + 1)`, which holds for about `1 / (N + 1)` of keys. So roughly `N / (N + 1)` of all keys move: 91% at ten nodes, 99% at a hundred. Removing a failed node is just as bad. The goal of every scheme below is the opposite: move only about `1 / (N + 1)` of keys when a node is added, and only the departed node's keys when one is removed.

## Consistent hashing on a ring

Karger et al. (1997) proposed it for web caching. Map both nodes and keys into the same hash space, say 0 to 2^64 - 1, treated as a circle. A key belongs to the first node position at or after its hash, walking clockwise.

```text
 Hash space 0 .. 2^64-1, left to right, wrapping at the end.
 A key belongs to the first node position at or after its hash.

 before:  ---A1----k1---B1---k2----k3----C1----k4---A2---
                  k1->B1      k2,k3->C1     k4->A2

 add D1:  ---A1----k1---B1---k2--D1--k3--C1----k4---A2---
                                k2->D1  k3->C1 (unchanged)

 Only keys in the arc (B1, D1] move, and they all move to D1.
```

Adding a node takes over one arc from its successor; nothing else changes. Removing a node hands its arc to its successor. Lookup is a binary search over sorted positions, `O(log P)` for `P` positions.

### Virtual nodes

With one position per node, arcs are wildly uneven. For `N` nodes the largest arc is about `ln N` times the average, so at ten nodes the busiest node holds over twice its fair share. Worse, when a node dies its entire arc lands on one neighbor, which may then fall over too.

Virtual nodes (vnodes, or tokens) fix this: each physical node gets `V` positions, `hash("cache-3#0")` through `hash("cache-3#99")`. Each node's load is now the sum of many small arcs, so the spread shrinks roughly as `1 / sqrt(V)`.

What vnodes buy:

- **Balance.** 100 to 200 vnodes per node typically brings max/avg load within 10–15%.
- **Weighted capacity.** A node with twice the RAM gets twice the vnodes.
- **Graceful failure.** A dead node's arcs are scattered, so its load spreads over all survivors instead of one neighbor.
- **Faster rebuilds.** A new node pulls data from many peers in parallel.

What they cost: `N * V` ring entries to store and gossip, and more ranges to stream and repair. Cassandra illustrates the tension: it defaulted to 256 tokens per node for years, then lowered the default to 16 in 4.0 alongside a token allocation algorithm that keeps balance at the lower count, because many tokens made repairs slower and made any two-node failure touch more ranges.

### Rebalancing cost

With `K` keys:

| Event | Modulo hashing | Ring with vnodes |
|---|---|---|
| Add 1 node to `N` | about `K * N / (N + 1)` keys move | about `K / (N + 1)` keys move, all to the new node |
| Remove 1 node | about `K * N / (N + 1)` keys move | only its `K / N` keys move, spread over survivors |
| Change hash function or vnode naming | everything moves | everything moves |

"Keys move" means real work. For a cache, moved keys are misses that hit the origin. For a database, moved ranges are streamed over the network while serving traffic, so rebalancing is throttled and can take hours for large nodes. Even the ideal `1 / (N + 1)` is a lot of data when nodes hold terabytes, which is why stores add capacity a node at a time and in off-peak windows.

### Replication on the ring

Dynamo-style stores replicate a key to the next `R` *distinct physical* nodes clockwise, called the preference list. Walking vnodes naively can place two replicas on the same machine, so the walk skips vnodes of nodes already chosen, and production systems also skip nodes in the same rack or availability zone. Quorum reads and writes over that preference list are covered in [CAP, consistency and PACELC]({{ '/backend/cap-and-consistency-models/' | relative_url }}).

## Rendezvous (HRW) hashing

Highest random weight hashing (Thaler and Ravishankar) skips the ring entirely. For a key, compute `score = hash(node, key)` for every node and pick the node with the highest score.

- **Minimal movement.** Adding a node moves only the keys for which the new node scores highest, about `1 / (N + 1)`. Removing a node moves only the keys it owned, each to its own second choice, so the load spreads evenly without vnodes.
- **Replicas for free.** The top `k` scores are the replica set, and they are distinct by construction.
- **No state.** No ring to build or store; each client needs only the node list.
- **Cost.** `O(N)` hashes per lookup. Trivial for tens of nodes, which covers most cache clusters and shard routers; for thousands, use a hierarchical variant or a ring.
- **Weights.** Weighted rendezvous uses `score = -weight / ln(u)`, where `u` is the hash mapped into `(0, 1)`.

Rendezvous hashing is a good default for client-side routing over a small, changing pool: memcached clients, CDN edge selection, routing jobs to workers.

## Jump consistent hash

Lamping and Veach (Google, 2014) published a five-line function that maps a 64-bit key to a bucket in `[0, n)`: no memory, about `O(ln n)` time, near-perfect balance, and when `n` grows to `n + 1` exactly `1 / (n + 1)` of keys move, all into the new bucket.

The catch: buckets are numbers, not names. You can only add or remove the *last* bucket. If bucket 3 of 10 dies, you cannot remove it without renumbering. That makes jump hash a fit for mapping keys to numbered shards that are themselves replicated and never disappear (storage shards, partitions), and a poor fit for a cache pool where any server can fail.

## Consistent hashing with bounded loads

Even with good balance on average, real traffic is skewed. Mirrokni, Thorup and Zadimoghaddam (SODA 2018) add a capacity cap: each node accepts at most `ceil((1 + eps) * average)` keys or in-flight requests. If a key's node is full, walk clockwise to the next node with spare capacity. This guarantees no node exceeds `(1 + eps)` times the average, while keeping movement on membership changes bounded (the paper shows it grows as `eps` shrinks, so `eps` around 0.25 is a common choice).

Vimeo publicly described adopting it in HAProxy to balance video cache traffic; HAProxy exposes it as `hash-balance-factor`. It is the right tool when you need affinity (same key to same server, for cache locality) and also need protection from overload.

## Choosing a scheme

| Scheme | Lookup | State | Balance | Movement on change | Remove any node | Typical use |
|---|---|---|---|---|---|---|
| `hash % N` | `O(1)` | None | Even | Almost all keys | Yes, at full cost | Fixed fleets only |
| Ring + vnodes | `O(log NV)` | `N*V` points | Good at V of 100+ | About `1/N` | Yes | Dynamo, Cassandra, ketama clients |
| Rendezvous | `O(N)` | Node list | Good | About `1/N` | Yes | Client routing, small pools |
| Jump hash | `O(log N)` | None | Near perfect | Exactly `1/(N+1)` | Last bucket only | Numbered shards |
| Maglev | `O(1)` | Lookup table | Near perfect | Small, not minimal | Yes | L4 balancers (Google, Envoy) |
| Fixed slots | `O(1)` | Slot map | As assigned | Whole slots, planned | Yes | Redis Cluster, Kafka partitions |

Fixed slots are the other big family: hash into many more partitions than nodes (Redis Cluster uses `CRC16(key) mod 16384`), then assign partitions to nodes explicitly. Rebalancing moves whole slots under operator or controller control. See [partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}).

## In practice

A runnable ring with virtual nodes, a preference list, bounded loads, rendezvous hashing and jump hash, measuring key movement and balance on 50,000 keys:

```python
import bisect
import hashlib
import math
from collections import Counter


def h64(s: str) -> int:
    """Stable 64-bit hash. Never use built-in hash(): it is salted per process."""
    return int.from_bytes(hashlib.md5(s.encode()).digest()[:8], "big")


class HashRing:
    def __init__(self, nodes=(), vnodes=100):
        self.vnodes = vnodes
        self.points = []  # sorted vnode positions on the ring
        self.owner = {}   # position -> physical node
        for n in nodes:
            self.add(n)

    def add(self, node):
        for i in range(self.vnodes):
            p = h64(f"{node}#{i}")
            bisect.insort(self.points, p)
            self.owner[p] = node

    def remove(self, node):
        for i in range(self.vnodes):
            p = h64(f"{node}#{i}")
            self.points.pop(bisect.bisect_left(self.points, p))
            del self.owner[p]

    def walk(self, key):
        """Yield distinct physical nodes clockwise from the key's position."""
        start = bisect.bisect(self.points, h64(key))
        seen = set()
        for j in range(len(self.points)):
            node = self.owner[self.points[(start + j) % len(self.points)]]
            if node not in seen:
                seen.add(node)
                yield node

    def get(self, key):
        return next(self.walk(key))

    def replicas(self, key, n):
        """Preference list: n distinct physical nodes (skip sibling vnodes)."""
        walk = self.walk(key)
        return [next(walk) for _ in range(n)]


def bounded_assign(ring, keys, nodes, eps=0.25):
    """Consistent hashing with bounded loads: skip nodes at capacity."""
    cap = math.ceil((1 + eps) * len(keys) / len(nodes))
    load, where = Counter(), {}
    for k in keys:
        node = next(n for n in ring.walk(k) if load[n] < cap)
        load[node] += 1
        where[k] = node
    return where, cap


def rendezvous(key, nodes):
    """Highest random weight: every node scores the key, highest score wins."""
    return max(nodes, key=lambda n: h64(f"{n}|{key}"))


def jump_hash(key: int, buckets: int) -> int:
    """Lamping and Veach jump consistent hash: buckets are 0..buckets-1."""
    b, j = -1, 0
    while j < buckets:
        b = j
        key = (key * 2862933555777941757 + 1) & 0xFFFFFFFFFFFFFFFF
        j = int((b + 1) * ((1 << 31) / ((key >> 33) + 1)))
    return b


def moved(before, after):
    return sum(before[k] != after[k] for k in before) / len(before)


def imbalance(assignment):
    counts = Counter(assignment.values())
    return max(counts.values()) / (len(assignment) / len(counts))


if __name__ == "__main__":
    keys = [f"user:{i}" for i in range(50_000)]
    nodes = [f"cache-{i}" for i in range(10)]
    grown = nodes + ["cache-10"]

    # 1. Modulo hashing remaps almost everything when N changes.
    mod_b = {k: h64(k) % 10 for k in keys}
    mod_a = {k: h64(k) % 11 for k in keys}
    print(f"mod-N        moved {moved(mod_b, mod_a):.3f}")
    assert moved(mod_b, mod_a) > 0.85

    # 2. Ring with vnodes moves about 1/11 of keys, all onto the new node.
    ring = HashRing(nodes, vnodes=100)
    ring_b = {k: ring.get(k) for k in keys}
    ring.add("cache-10")
    ring_a = {k: ring.get(k) for k in keys}
    print(f"ring+vnodes  moved {moved(ring_b, ring_a):.3f}")
    assert 0.05 < moved(ring_b, ring_a) < 0.13
    assert all(ring_a[k] == "cache-10" for k in keys if ring_a[k] != ring_b[k])

    # Removing a node moves only that node's keys, spread over many nodes.
    ring.remove("cache-3")
    ring_r = {k: ring.get(k) for k in keys}
    lost = [k for k in keys if ring_a[k] == "cache-3"]
    assert all(ring_r[k] == ring_a[k] for k in keys if ring_a[k] != "cache-3")
    print(f"cache-3 keys went to {len(Counter(ring_r[k] for k in lost))} nodes")

    # 3. Virtual nodes fix the balance.
    skew = {}
    for v in (1, 10, 200):
        r = HashRing(nodes, vnodes=v)
        skew[v] = imbalance({k: r.get(k) for k in keys})
        print(f"vnodes={v:<4} max/avg load {skew[v]:.2f}")
    assert skew[200] < 1.2 < skew[1]

    # 4. Bounded loads cap the hottest node even with a badly balanced ring.
    where, cap = bounded_assign(HashRing(nodes, vnodes=1), keys, nodes)
    assert max(Counter(where.values()).values()) <= cap
    print(f"bounded      max/avg load {imbalance(where):.2f} (cap {cap})")

    # 5. Rendezvous and jump hash: same minimal movement, no ring to store.
    hrw_b = {k: rendezvous(k, nodes) for k in keys}
    hrw_a = {k: rendezvous(k, grown) for k in keys}
    jmp_b = {k: jump_hash(h64(k), 10) for k in keys}
    jmp_a = {k: jump_hash(h64(k), 11) for k in keys}
    print(f"rendezvous   moved {moved(hrw_b, hrw_a):.3f}")
    print(f"jump hash    moved {moved(jmp_b, jmp_a):.3f}, "
          f"max/avg {imbalance(jmp_a):.2f}")
    assert 0.07 < moved(hrw_b, hrw_a) < 0.11
    assert 0.07 < moved(jmp_b, jmp_a) < 0.11
    assert all(jmp_a[k] == 10 for k in keys if jmp_a[k] != jmp_b[k])

    # 6. Replicas land on distinct physical nodes.
    assert len(set(ring.replicas("order:42", 3))) == 3
    print("ok")
```

Typical output: modulo hashing moves 0.908 of keys; the ring moves about 0.10 (the ideal is 0.091; the new node's 100 vnodes happen to claim slightly more than a fair share); rendezvous and jump hash move 0.092. Max/avg load is 2.29 with one vnode per node, 1.46 with ten, 1.10 with 200, and bounded loads holds it at 1.25 even on the one-vnode ring.

Where you meet these in real config (illustrative):

```nginx
# nginx: ketama-style consistent hashing for a cache tier
upstream render_cache {
    hash $request_uri consistent;
    server cache-1:8080;
    server cache-2:8080;
    server cache-3:8080;
}
```

```text
# HAProxy: consistent hashing with bounded loads (125 = 1.25 x average)
backend video_cache
    balance uri
    hash-type consistent
    hash-balance-factor 125
    server c1 10.0.2.11:80 check
    server c2 10.0.2.12:80 check
```

```python
# Django: several memcached servers. PyMemcacheCache hands the list to
# pymemcache's HashClient, which routes keys client-side (rendezvous hashing
# by default in recent versions; check yours).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.memcached.PyMemcacheCache",
        "LOCATION": ["10.0.3.11:11211", "10.0.3.12:11211", "10.0.3.13:11211"],
    }
}
```

## Pitfalls

- **Using Python's `hash()`.** String hashing is salted per process (`PYTHONHASHSEED`), so two workers place the same key on different nodes. Use a stable hash such as MD5, xxHash or MurmurHash.
- **Clients disagree on membership.** One client sees nine nodes, another ten; the same key is cached twice and invalidations miss one copy. Distribute membership from one source (service discovery, cluster gossip) and keep cache TTLs bounded.
- **Too few vnodes.** One node holds 2x its share and is the first to run out of memory. Measure max/avg load before going to production.
- **Replicas on the same machine.** A naive clockwise walk returns sibling vnodes. Skip by physical node, then by rack or zone.
- **Hot keys.** Consistent hashing balances key counts, not request rates. One celebrity key still lands on one node. Fix with a local in-process cache, key splitting (`key#0..key#7` with reads from a random suffix), or replicating hot keys.
- **Cascading failure.** With few positions, a dead node's load hits one neighbor, which then dies. Vnodes and bounded loads both prevent the domino.
- **Cold new nodes.** A new cache node starts empty and turns its whole share into misses at once. Ramp its weight (vnode count) up over minutes.
- **Changing the vnode naming scheme or hash function** is a full reshuffle. Treat it like a migration.

## Interview questions

**Why consistent hashing instead of `hash % N`?** Because changing `N` remaps about `N / (N + 1)` of keys with modulo, but only about `1 / (N + 1)` with consistent hashing, and removing a node moves only that node's keys.

**What do virtual nodes solve?** Uneven arc sizes with one position per node (the busiest node gets about `ln N` times its share), concentrated load on one neighbor after a failure, and heterogeneous capacity via per-node vnode counts.

**How does a Dynamo-style store choose replicas?** Hash the key onto the ring and walk clockwise, taking the next `R` distinct physical nodes, ideally in different racks or zones. That list is the preference list for quorum reads and writes.

**Ring or rendezvous hashing?** Rendezvous is simpler, needs no ring, gives replica sets for free and spreads a failed node's keys evenly, at `O(N)` per lookup. A ring is better when `N` is large or lookups are very hot.

**When would you use jump hash?** To map keys onto a fixed set of numbered, replicated shards where only the count grows. Not for pools where arbitrary members fail.

**Does consistent hashing handle hot keys?** No. It balances the key space, not traffic. Hot keys need replication, splitting, or local caching; bounded loads help by spilling overflow to the next node.

**How does Redis Cluster distribute keys?** It is fixed-slot partitioning, not a ring: `CRC16(key) mod 16384` picks a slot, and each master owns a set of slots. Resharding migrates slots between nodes. Hash tags (`{user:42}`) force related keys into one slot.

## Further reading

- *Consistent Hashing and Random Trees: Distributed Caching Protocols for Relieving Hot Spots on the World Wide Web*, Karger et al. (STOC 1997).
- *Dynamo: Amazon's Highly Available Key-value Store*, DeCandia et al. (SOSP 2007).
- *A Fast, Minimal Memory, Consistent Hash Algorithm*, John Lamping and Eric Veach (2014).
- *Consistent Hashing with Bounded Loads*, Mirrokni, Thorup and Zadimoghaddam (SODA 2018).
- *Using Name-Based Mappings to Increase Hit Rates*, David Thaler and Chinya Ravishankar (IEEE/ACM Transactions on Networking, 1998).
