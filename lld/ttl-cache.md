---
title: Design a cache with expiry (TTL)
section: lld
order: 25
phase: "Phase 5 · Intermediate"
summary: A TTL cache with lazy and active expiry, a min-heap of expiry times, LRU eviction when full, an injectable clock for tests, and thread safety.
---

## Requirements

Clarifying questions to ask first:

- One default TTL, or a TTL per entry?
- Does reading a key extend its life (expire-after-access) or not (expire-after-write)?
- Is there also a capacity limit? Which entry goes when full and nothing has expired?
- Must expired entries free memory promptly, or only when touched?
- Multiple threads? How precise must expiry be (milliseconds or seconds)?

Functional:

- `put(key, value, ttl=None)` stores with a per-entry or default TTL; re-putting resets the TTL.
- `get(key)` returns the value only if not expired; an expired key behaves as missing.
- Capacity limit: when full, drop expired entries first, then evict the least recently used.
- `delete(key)`, `purge_expired()`, and hit, miss, expired and evicted counters.
- An optional background sweeper removes expired entries nobody reads.

Non-functional: `get` in `O(1)`, `put` in `O(log n)`; thread-safe; deterministic tests with no `sleep`.

Out of scope: expire-after-access (follow-up), size in bytes, distribution.

## Entities and relationships

```text
+------------------------------------+       +----------------------+
| TTLCache[K, V]                     | 1   * | _Entry[V]            |
| capacity, ttl                      |------>| value: V             |
| clock: () -> float   (injected)    |       | expires_at: float    |
| _data: OrderedDict[K, _Entry]      |       +----------------------+
|        (LRU order, oldest first)   |
| _heap: [(expires_at, seq, key)]    |
| _lock: Lock                        |
| get, put, delete, purge_expired    |
+------------------+-----------------+
                   ^ calls purge_expired() every interval
                   |
+------------------+-----------------+       +----------------------+
| Sweeper                            |       | FakeClock (tests)    |
| interval, _stop: Event, _thread    |       | now, advance(s)      |
+------------------------------------+       +----------------------+
```

Two indexes over the same entries: `_data` answers "what is the value, and which key is least recently used?", the heap answers "which key expires next?".

## Patterns and why

| Pattern / idiom | Where | Why (what it prevents) |
|-----------------|-------|------------------------|
| Dependency injection | `clock` parameter | Tests that `sleep()` and flake; `FakeClock` makes time explicit |
| Lazy deletion in a heap | Stale `(expires_at, key)` entries skipped on pop | `O(n)` removal from the middle of a heap on every overwrite |
| Strategy (extension) | Expiry policy: after-write, after-access | Hard-coding one expiry rule into `get` and `put` |
| Single responsibility | `Sweeper` is separate | The cache stays usable (and testable) without a thread |

## Lazy vs active expiry

| | Lazy (on read) | Active (heap sweep) |
|-|----------------|---------------------|
| When | `get` finds `expires_at <= now` | `put` and the sweeper pop due heap entries |
| Cost | `O(1)`, only for keys you touch | `O(log n)` per expired key |
| Gap it leaves | Keys never read again stay in memory | Up to one sweep interval of staleness |

Use both: lazy guarantees you never return a stale value; active frees memory. Redis does the same (lazy on access plus periodic random sampling).

## Code

```python
import heapq
import itertools
import random
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry[V]:
    value: V
    expires_at: float


class TTLCache[K: Hashable, V]:
    """Every entry expires after its TTL; when full, LRU is evicted."""

    def __init__(self, capacity: int, ttl: float,
                 clock: Callable[[], float] = time.monotonic):
        if capacity <= 0 or ttl <= 0:
            raise ValueError("capacity and ttl must be positive")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._data: OrderedDict[K, _Entry[V]] = OrderedDict()   # LRU first
        self._heap: list[tuple[float, int, K]] = []  # (expires_at, seq, key)
        self._seq = itertools.count()                # tie-break: keys may not compare
        self._lock = threading.Lock()
        self.stats = dict(hits=0, misses=0, expired=0, evicted=0)

    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            e = self._data.get(key)
            if e is not None and e.expires_at <= self.clock():
                del self._data[key]                  # lazy expiry on read
                self.stats["expired"] += 1
                e = None
            if e is None:
                self.stats["misses"] += 1
                return default
            self._data.move_to_end(key)              # refresh LRU, not TTL
            self.stats["hits"] += 1
            return e.value

    def put(self, key: K, value: V, ttl: float | None = None) -> None:
        ttl = self.ttl if ttl is None else ttl
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        with self._lock:
            now = self.clock()
            e = self._data[key] = _Entry(value, now + ttl)
            self._data.move_to_end(key)
            heapq.heappush(self._heap, (e.expires_at, next(self._seq), key))
            self._purge(now)             # free expired slots before evicting live keys
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)       # LRU eviction
                self.stats["evicted"] += 1
            if len(self._heap) > 2 * len(self._data) + 64:
                self._compact()          # overwrites and evictions leave stale entries

    def delete(self, key: K) -> bool:
        with self._lock:                 # its heap entry goes stale; skipped later
            return self._data.pop(key, None) is not None

    def purge_expired(self, limit: int = 1000) -> int:
        with self._lock:
            return self._purge(self.clock(), limit)

    def __len__(self) -> int:            # may include expired keys not yet purged
        return len(self._data)

    def _purge(self, now: float, limit: int = 1000) -> int:
        removed = 0
        for _ in range(limit):           # bounded: never hold the lock for long
            if not self._heap or self._heap[0][0] > now:
                break
            exp, _, key = heapq.heappop(self._heap)
            e = self._data.get(key)
            if e is not None and e.expires_at == exp:   # else stale: rewritten or gone
                del self._data[key]
                removed += 1
        self.stats["expired"] += removed
        return removed

    def _compact(self) -> None:
        self._heap = [(e.expires_at, next(self._seq), k) for k, e in self._data.items()]
        heapq.heapify(self._heap)


class Sweeper:
    """Active expiry: frees memory held by keys nobody reads again."""

    def __init__(self, cache: TTLCache, interval: float = 1.0):
        self.cache, self.interval = cache, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Sweeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):    # sleeps; wakes at once on stop
            self.cache.purge_expired()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


if __name__ == "__main__":
    clock = FakeClock()
    c = TTLCache[str, int](capacity=3, ttl=10, clock=clock)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    clock.advance(5)
    assert c.get("a") == 1                   # hit: "a" becomes most recent
    c.put("d", 4)                            # full, nothing expired: evict LRU "b"
    assert c.get("b") is None and c.stats["evicted"] == 1
    clock.advance(5)                         # t=10: a and c (written at t=0) expire
    assert c.get("a") is None                # lazy expiry on read
    assert c.purge_expired() == 1            # active expiry removes "c"
    assert len(c) == 1 and c.get("d") == 4
    c.put("short", 0, ttl=1)
    clock.advance(1)
    assert c.get("short") is None

    # real clock + sweeper: memory is freed without any get()
    live = TTLCache[int, int](capacity=1000, ttl=0.05)
    sweeper = Sweeper(live, interval=0.01).start()
    for i in range(100):
        live.put(i, i)
    time.sleep(0.2)
    sweeper.stop()
    assert len(live) == 0

    sys.setswitchinterval(1e-6)                  # switch threads often so races show up
    # stress: 8 threads; capacity holds and every live key is in the heap
    shared = TTLCache[int, int](capacity=50, ttl=0.005)

    def work(seed: int) -> None:
        rnd = random.Random(seed)
        for _ in range(3000):
            k = rnd.randrange(200)
            if rnd.random() < 0.5:
                shared.put(k, k)
            else:
                assert shared.get(k) in (None, k)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(shared) <= 50
    in_heap = {(exp, k) for exp, _, k in shared._heap}
    assert all((e.expires_at, k) in in_heap for k, e in shared._data.items())
    print("ok")
```

## Key flows

`put("d", 4)` when full:

```text
caller      TTLCache         _data (LRU)          _heap
  | put(d)       |                |                  |
  |------------->| lock; now = clock()               |
  |              |--_data[d] = Entry; move_to_end--->|
  |              |--heappush((now+ttl, seq, d))----->|
  |              | _purge(now): pop while top <= now |
  |              |   stale entry? skip; else delete  |
  |              | still over capacity?              |
  |              |--popitem(last=False)->| evict LRU |
  |              | heap > 2x data? rebuild heap      |
  |<-------------| unlock         |                  |
```

`get("a")` after expiry:

```text
caller      TTLCache         _data
  | get(a)       |                |
  |------------->| lock           |
  |              |--lookup a----->|
  |              | expires_at <= clock()? yes
  |              |--del a-------->|   (heap entry is now stale)
  |<-- default --| unlock         |
```

## Edge cases

- Expiry is inclusive: an entry written at `t=0` with `ttl=10` is gone at `t=10`.
- Re-putting a key pushes a second heap entry; the old one no longer matches `expires_at` and is skipped. Without that check, the old entry would delete the fresh value.
- Keys need not be comparable: `seq` breaks ties so the heap never compares keys.
- `__len__` can count expired keys that nobody has touched yet; call `purge_expired()` first if you need an exact count.
- `ttl <= 0` is rejected; "do not cache" belongs in the caller.
- `get` refreshes LRU position but not the TTL (expire-after-write). Choose deliberately.
- Use `time.monotonic()`: wall-clock time jumps under NTP corrections and would expire everything at once, or nothing.

## Production follow-ups

**Is it thread-safe?** One lock guards both indexes because they must change together; a reader must never see a key in `_data` without its heap entry. Even `get` writes (LRU reorder, lazy delete), so a read-write lock buys nothing. The sweeper takes the same lock but purges at most `limit` entries per call, so it never stalls requests for long. The GIL does not make "check expiry, then delete" atomic.

**What about persistence and restart?** Monotonic time is meaningless after a restart. When persisting, store absolute wall-clock expiry (Unix ms, as Redis does) and drop already-expired entries on load. Most in-process TTL caches simply start cold.

**What about retries and stampedes?** `put` is idempotent apart from resetting the TTL. The real problem is a hot key expiring and 500 requests recomputing it at once. Fixes: single-flight loading (one loader per key, others wait), jitter on TTLs (`ttl * uniform(0.9, 1.1)`) so keys written together do not expire together, and stale-while-revalidate (serve the old value while one caller refreshes).

**How do you add expire-after-access or a different eviction policy?** Make expiry a strategy: `on_write(entry, now)` and `on_read(entry, now)` decide `expires_at`. Expire-after-access pushes a new heap entry on each read; the stale-entry check already handles that. Eviction (LRU, LFU) is a separate strategy over `_data`.

**What breaks at 10× or across machines?** Each `put` is `O(log n)` and stale heap entries cost memory until compaction. At millions of timers, a timing wheel (one bucket per tick, hierarchical for long delays) gives `O(1)` insert and expiry; Kafka and Netty use timing wheels for this. Across machines, use Redis `SET key value EX ttl`; it combines lazy expiry with sampling about 20 TTL keys at a time and repeating while more than a quarter are expired.

**What would you measure?** Hit ratio; expired vs evicted counts (many evictions of live keys means the cache is too small; mostly expirations means TTL is too short); heap size vs entry count; sweeper run time and entries per run.

**What about clock skew?** In-process, monotonic time avoids jumps. In a distributed cache, compute expiry on the server that stores the key, never from a client's clock, or two app servers with skewed clocks will disagree about what is fresh.

## Interview follow-ups

- **Why a heap instead of scanning all keys?** A scan is `O(n)` per sweep, even when nothing expired. The heap's top tells you in `O(1)` whether anything is due.
- **Why not delete the old heap entry when a key is overwritten?** Removing from the middle of a heap is `O(n)`. Skipping stale entries on pop is `O(log n)`, with periodic compaction to cap memory.
- **Can one structure do both?** Only if every entry has the same TTL and reads do not reorder: then insertion order equals expiry order and one `OrderedDict` handles eviction and expiry from the front. Per-entry TTLs, or LRU plus TTL, need the second index.
- **How do you test it without `sleep`?** Inject the clock. `FakeClock.advance(10)` makes "ten seconds later" instant and deterministic.
