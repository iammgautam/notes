---
title: Design an LRU cache
section: lld
order: 24
phase: "Phase 5 · Intermediate"
summary: O(1) LRU cache with a hashmap and doubly linked list, a thread-safe version with a stress test, the OrderedDict shortcut, and scaling follow-ups.
---

## Requirements

Clarifying questions to ask first:

- Capacity counted in entries or in bytes?
- What does a miss return: `None`, a default, or should the cache load the value?
- Does `put` on an existing key count as a use? (Yes, almost always.)
- Called from multiple threads?
- Any expiry? (If yes, see [Design a cache with expiry (TTL)]({{ '/lld/ttl-cache/' | relative_url }}).)

Functional:

- `get(key)` returns the value or a default, and marks the key most recently used.
- `put(key, value)` inserts or updates, and marks it most recently used.
- When full, inserting a new key evicts the least recently used one.
- `delete(key)`; hit, miss and eviction counters.

Non-functional: `get` and `put` in `O(1)`; safe under concurrent access.

Out of scope: TTL, size-in-bytes accounting, loading on miss (follow-ups).

## Entities and relationships

```text
+--------------------------------+         +-------------------------+
| LRUCache[K, V]                 | 1     * | _Node                   |
| capacity: int                  |-------->| key, val                |
| _map: dict[K, _Node]           | indexes | prev, next: _Node       |
| _head, _tail: _Node (sentinel) |         +-------------------------+
| _lock: Lock                    |
| hits, misses, evictions        |
| get, put, delete, keys         |
+--------------------------------+

 _map:      k3 --+        k1 --+        k7 --+
                 v             v             v
 head <-> [k3 | v] <-> [k1 | v] <-> [k7 | v] <-> tail
          MRU                       LRU: evicted next
```

- The **dict** finds a node in `O(1)`.
- The **doubly linked list** keeps recency order. With a pointer to a node you can unlink it and move it to the front in `O(1)`; a singly linked list cannot unlink without its predecessor.
- **Sentinel** head and tail nodes mean no `None` checks for empty-list or end-of-list cases.

## Patterns and why

| Pattern / idiom | Where | Why (what it prevents) |
|-----------------|-------|------------------------|
| Dict + intrusive list | `_map` + `_Node` | Neither alone gives `O(1)` for both lookup and reorder |
| Sentinel nodes | `_head`, `_tail` | Edge-case bugs on empty list, first and last node |
| Monitor (Java `synchronized`) | `_lock` around every method | Half-rewired pointers seen by another thread |
| Strategy (extension) | `EvictionPolicy`: LRU, LFU, FIFO | Changing policy without rewriting storage |

## Code

```python
import random
import sys
import threading
from collections.abc import Hashable


class _Node:
    __slots__ = ("key", "val", "prev", "next")

    def __init__(self, key=None, val=None):
        self.key, self.val = key, val
        self.prev: _Node | None = None
        self.next: _Node | None = None


class LRUCache[K: Hashable, V]:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._map: dict[K, _Node] = {}
        self._head, self._tail = _Node(), _Node()      # sentinels
        self._head.next, self._tail.prev = self._tail, self._head
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    # ---- list helpers (caller holds the lock) ----
    def _unlink(self, n: _Node) -> None:
        n.prev.next, n.next.prev = n.next, n.prev

    def _push_front(self, n: _Node) -> None:
        n.prev, n.next = self._head, self._head.next
        self._head.next.prev = n
        self._head.next = n

    # ---- public API ----
    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:                  # get rewires pointers: it is a write
            node = self._map.get(key)
            if node is None:
                self.misses += 1
                return default
            self.hits += 1
            self._unlink(node)
            self._push_front(node)
            return node.val

    def put(self, key: K, val: V) -> None:
        with self._lock:
            node = self._map.get(key)
            if node is not None:          # update: no eviction needed
                node.val = val
                self._unlink(node)
                self._push_front(node)
                return
            if len(self._map) == self.capacity:
                lru = self._tail.prev
                self._unlink(lru)
                del self._map[lru.key]    # why the node stores its key
                self.evictions += 1
            node = _Node(key, val)
            self._map[key] = node
            self._push_front(node)

    def delete(self, key: K) -> bool:
        with self._lock:
            node = self._map.pop(key, None)
            if node is None:
                return False
            self._unlink(node)
            return True

    def keys(self) -> list[K]:           # MRU -> LRU, for tests and debugging
        with self._lock:
            out, n = [], self._head.next
            while n is not self._tail:
                out.append(n.key)
                n = n.next
            return out

    def __len__(self) -> int:
        return len(self._map)


if __name__ == "__main__":
    c = LRUCache[int, str](2)
    c.put(1, "a")
    c.put(2, "b")
    assert c.get(1) == "a"                # 1 is now MRU
    c.put(3, "c")                         # evicts 2, the LRU
    assert c.get(2) is None and c.evictions == 1
    c.put(1, "A")                         # update in place, no eviction
    assert c.get(1) == "A" and c.get(3) == "c" and c.keys() == [3, 1]
    assert c.delete(3) and not c.delete(3) and c.keys() == [1]

    sys.setswitchinterval(1e-6)                  # switch threads often so races show up
    # stress: 8 threads of random get/put/delete; list and map must agree
    shared = LRUCache[int, int](50)

    def work(seed: int) -> None:
        rnd = random.Random(seed)
        for _ in range(5000):
            k, op = rnd.randrange(200), rnd.random()
            if op < 0.5:
                shared.put(k, k * 10)
            elif op < 0.9:
                assert shared.get(k) in (None, k * 10)
            else:
                shared.delete(k)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    order = shared.keys()
    assert len(order) == len(shared) <= 50
    assert set(order) == set(shared._map)
    print("ok")
```

### The `OrderedDict` shortcut

`collections.OrderedDict` is a dict plus a doubly linked list, with `move_to_end` and `popitem(last=False)` in `O(1)`. Mention it first; implement by hand if the interviewer wants the internals.

```python
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity: int):
        self.capacity, self.d = capacity, OrderedDict()

    def get(self, key, default=None):
        if key not in self.d:
            return default
        self.d.move_to_end(key)           # mark MRU
        return self.d[key]

    def put(self, key, val) -> None:
        self.d[key] = val
        self.d.move_to_end(key)
        if len(self.d) > self.capacity:
            self.d.popitem(last=False)    # drop LRU (front)


c = LRUCache(2)
c.put("a", 1)
c.put("b", 2)
c.get("a")
c.put("c", 3)
assert list(c.d) == ["a", "c"]
```

For memoizing a pure function, `functools.lru_cache(maxsize=1024)` (or `functools.cache` when unbounded is fine) is the production answer. It is thread-safe in the sense that it will not corrupt itself, but two threads can still compute the same missing value at the same time.

## Key flows

`get(1)` on a hit:

```text
caller       LRUCache          _map            linked list
  | get(1)       |               |                  |
  |------------->| lock          |                  |
  |              |--lookup 1---->|                  |
  |              |<---node-------|                  |
  |              |--unlink(node)------------------->|
  |              |--push_front(node)--------------->| node is MRU
  |<---- val ----| unlock        |                  |
```

`put(3, "c")` when full:

```text
caller       LRUCache          _map            linked list
  | put(3)       |               |                  |
  |------------->| lock; 3 absent, len == capacity  |
  |              |--lru = tail.prev---------------->|
  |              |--unlink(lru)-------------------->|
  |              |--del _map[lru.key]->|            |
  |              |--_map[3] = node---->|            |
  |              |--push_front(node)--------------->|
  |<-------------| unlock        |                  |
```

## Edge cases

- Updating an existing key must not evict anything, even when full: check membership first.
- The node must store its key, or eviction cannot remove the dict entry.
- `capacity <= 0` is rejected at construction.
- A cached `None` is indistinguishable from a miss when `default=None`; pass a sentinel default if `None` is a legal value.
- Evict when `len == capacity` before inserting (or when `len > capacity` after). Mixing the two is an off-by-one.

## Production follow-ups

**Is it thread-safe, and why not a read-write lock?** Every operation holds one lock, because even `get` rewires pointers; a read-write lock (Java `ReentrantReadWriteLock`) gives no benefit when all calls write. The GIL makes single bytecodes atomic, not the four pointer assignments in `_unlink` + `_push_front`. For more throughput, shard: N independent caches selected by `hash(key) % N`, each with its own lock (Java's old `ConcurrentHashMap` segments). Caffeine (Java) goes further: it records reads in a buffer and applies the reordering in batches. See [Read-write lock cache]({{ '/lld/read-write-lock-cache/' | relative_url }}) for when an RW lock does pay off.

**What happens on restart?** The cache starts cold and the backing store takes the full load. Either accept a warm-up period, or snapshot the hottest N keys on shutdown and preload them on start. Protect the store during warm-up with request coalescing.

**How do you avoid a stampede when many threads miss the same key?** Add `get_or_load(key, loader)` with single-flight: the first caller loads while others wait on a per-key `Future` or `Event`. Loads must be idempotent because a failed load is retried by the next caller.

**How do you add LFU or FIFO without editing the cache?** Split storage from policy: an `EvictionPolicy` with `on_access(key)`, `on_insert(key)`, `on_remove(key)` and `victim()`. LRU is the list above; LFU keeps `freq -> doubly linked list` plus `min_freq`, all `O(1)`.

**What breaks at 10× size or across machines?** Counting entries stops bounding memory when values vary in size: track bytes and evict until under budget. Across machines, use Redis with `maxmemory-policy allkeys-lru`; it approximates LRU by sampling a few keys rather than keeping a global list. Shard keys with consistent hashing so adding a node moves only `1/N` of keys.

**What would you measure?** Hit ratio (the number that justifies the cache), evictions per second (high means the cache is too small), size, and load latency on misses. A sudden hit-ratio drop after a deploy usually means a key format changed.

## Interview follow-ups

- **Why a doubly linked list and not a singly linked one?** Unlinking a node in `O(1)` needs its predecessor; only the doubly linked list has it.
- **Why store the key in the node?** On eviction you reach the node from the list tail and must delete its dict entry.
- **How would you add TTL?** Store `expires_at` per entry, check on `get` (lazy), and sweep with a min-heap of expiries. Full design in [Design a cache with expiry (TTL)]({{ '/lld/ttl-cache/' | relative_url }}).
- **LRU or LFU?** LRU adapts quickly to changing access patterns but a single full scan flushes it. LFU keeps long-term hot keys but holds stale popularity; real systems use hybrids such as W-TinyLFU.
