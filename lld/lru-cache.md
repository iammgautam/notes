---
title: Design an LRU cache
section: lld
order: 9
summary: O(1) get and put LRU cache with a hashmap plus doubly linked list, full Python code, the OrderedDict shortcut, thread safety and eviction-policy extensions.
---

## Requirements

- `get(key)`: return value or `None`/`-1`; marks key as most recently used.
- `put(key, value)`: insert or update; marks as most recently used.
- Fixed capacity; on overflow evict the **least recently used** key.
- Both operations `O(1)`.
- Follow-ups: thread safety, TTL, pluggable eviction policy.

## Core idea

- **Hashmap** `key → node` gives `O(1)` lookup.
- **Doubly linked list** keeps recency order: head = most recent, tail = least recent. With a node pointer you can unlink and relink in `O(1)`.
- Sentinel `head` and `tail` nodes remove all the empty-list edge cases.

```text
head <-> [k3] <-> [k1] <-> [k7] <-> tail
          MRU                 LRU (evict this)
```

## Full code

```python
import threading


class Node:
    __slots__ = ("key", "val", "prev", "next")

    def __init__(self, key=None, val=None):
        self.key, self.val = key, val
        self.prev = self.next = None


class LRUCache:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.cap = capacity
        self.map: dict = {}
        self.head, self.tail = Node(), Node()     # sentinels
        self.head.next, self.tail.prev = self.tail, self.head
        self._lock = threading.Lock()

    # list helpers
    def _remove(self, node: Node) -> None:
        node.prev.next, node.next.prev = node.next, node.prev

    def _add_front(self, node: Node) -> None:
        node.prev, node.next = self.head, self.head.next
        self.head.next.prev = node
        self.head.next = node

    # public API
    def get(self, key):
        with self._lock:                  # get mutates order, so lock it too
            node = self.map.get(key)
            if node is None:
                return None
            self._remove(node)
            self._add_front(node)
            return node.val

    def put(self, key, val) -> None:
        with self._lock:
            node = self.map.get(key)
            if node:
                node.val = val
                self._remove(node)
                self._add_front(node)
                return
            if len(self.map) == self.cap:
                lru = self.tail.prev
                self._remove(lru)
                del self.map[lru.key]     # why the node stores its key
            node = Node(key, val)
            self.map[key] = node
            self._add_front(node)


if __name__ == "__main__":
    c = LRUCache(2)
    c.put(1, "a"); c.put(2, "b")
    assert c.get(1) == "a"                # 1 is now MRU
    c.put(3, "c")                         # evicts 2
    assert c.get(2) is None
    c.put(1, "A")                         # update, no eviction
    assert c.get(1) == "A" and c.get(3) == "c"
```

## Complexity

| Op | Time | Space |
|----|------|-------|
| `get` | `O(1)` | |
| `put` | `O(1)` | `O(capacity)` total |

## OrderedDict shortcut

`collections.OrderedDict` is a dict backed by a doubly linked list, so it already does this. Mention it, then implement by hand if they want to see the internals.

```python
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity: int):
        self.cap, self.d = capacity, OrderedDict()

    def get(self, key):
        if key not in self.d:
            return None
        self.d.move_to_end(key)           # mark MRU
        return self.d[key]

    def put(self, key, val):
        self.d[key] = val
        self.d.move_to_end(key)
        if len(self.d) > self.cap:
            self.d.popitem(last=False)    # pop LRU (front)
```

For memoizing functions, `functools.lru_cache(maxsize=...)` is the production answer.

## Class design for extensibility

If asked for pluggable eviction (LRU, LFU, FIFO), split storage from policy:

```python
from abc import ABC, abstractmethod


class EvictionPolicy(ABC):
    @abstractmethod
    def accessed(self, key): ...
    @abstractmethod
    def evict(self): ...              # returns key to evict
    @abstractmethod
    def removed(self, key): ...


class Cache:
    def __init__(self, capacity: int, policy: EvictionPolicy):
        self.cap, self.policy, self.store = capacity, policy, {}

    def get(self, key):
        if key in self.store:
            self.policy.accessed(key)
            return self.store[key]
        return None

    def put(self, key, val):
        if key not in self.store and len(self.store) == self.cap:
            del self.store[self.policy.evict()]
        self.store[key] = val
        self.policy.accessed(key)
```

Pattern: **strategy** for eviction policy.

## Pitfalls

- Forgetting to store `key` in the node, so you cannot delete from the map on eviction.
- Not moving the node on `put` of an existing key.
- Evicting before checking whether the key already exists (evicts needlessly).
- Locking only `put`; `get` also rewires pointers.
- Off-by-one: evict when `len == cap` before inserting, or when `len > cap` after.

## Extensions interviewers ask

- **TTL:** store `expires_at` in the node; check on `get` (lazy expiry) plus optional background sweep.
- **LFU:** `key → node` plus `freq → doubly linked list`, track `min_freq`; all `O(1)`.
- **Higher concurrency:** shard into N caches by `hash(key) % N`, each with its own lock.
- **Distributed:** Redis with `maxmemory-policy allkeys-lru` (approximate LRU via sampling).
- **Write-through / write-back:** cache sits in front of a slow store; on evict, flush dirty entries.
