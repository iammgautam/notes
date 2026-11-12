---
title: A read-write lock cache
section: lld
order: 14
phase: "Phase 4 · Concurrency"
summary: Build a writer-preferring ReadWriteLock from Condition, use it in a cache that never tears reads, and know when a plain Lock is faster under the GIL.
---

A read-write lock lets many readers in at once but gives a writer exclusive access. Java ships `ReentrantReadWriteLock`; Python's stdlib has none, so "implement one" is a common machine-coding question. It pays off for read-heavy data where reads take real time.

## The readers-writers problem

Readers can share; a writer must be alone. The policy decides who waits:

| Policy | Rule | Starves |
|---|---|---|
| Reader preference | Readers enter whenever no writer holds the lock | Writers, if readers keep overlapping |
| Writer preference | A *waiting* writer blocks new readers | Readers, if writes never stop |
| Fair (FIFO) | Arrival order | Nobody, but more code |

Caches are read-heavy, so writer preference is the usual answer: a writer waits only for the readers already inside.

## Code

```python
import contextlib
import threading
import time
from contextlib import contextmanager


class ReadWriteLock:
    """Many readers or one writer. A waiting writer blocks new readers."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0               # threads holding the read lock
        self._writer = False            # a thread holds the write lock
        self._waiting_writers = 0       # > 0 means new readers must wait

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()  # let a waiting writer in

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()      # readers and writers re-check

    @contextmanager
    def read(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write(self):
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()


class RWCache:
    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = ReadWriteLock()

    def get(self, key, default=None):
        with self._lock.read():
            return self._data.get(key, default)

    def get_many(self, keys) -> list:
        with self._lock.read():          # one consistent view of several keys
            return [self._data.get(k) for k in keys]

    def put_many(self, pairs) -> None:
        with self._lock.write():         # readers never see half an update
            for key, value in pairs:
                self._data[key] = value

    def get_or_load(self, key, loader):
        with self._lock.read():
            if key in self._data:
                return self._data[key]
        value = loader(key)              # slow call, no lock held
        with self._lock.write():
            return self._data.setdefault(key, value)  # first one stored wins


class NoLock:                            # control: proves the test can see tearing
    def read(self):
        return contextlib.nullcontext()

    write = read


def stress(cache: RWCache, seconds: float = 0.3) -> tuple[int, int]:
    def pairs(x: int):
        yield "a", x
        time.sleep(0)                    # pause mid-update, lock still held
        yield "b", 100 - x               # invariant: a + b == 100

    cache.put_many(pairs(50))
    stop, reads, torn = threading.Event(), [], []

    def writer(x: int) -> None:
        while not stop.is_set():
            x = (x + 7) % 100
            cache.put_many(pairs(x))
            time.sleep(0.001)            # writes are rarer than reads

    def reader() -> None:
        while not stop.is_set():
            a, b = cache.get_many(["a", "b"])
            reads.append(a)
            if a + b != 100:
                torn.append((a, b))

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    ts += [threading.Thread(target=reader) for _ in range(6)]
    for t in ts:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in ts:
        t.join()
    return len(reads), len(torn)


def writer_wait_and_peak_readers(lock: ReadWriteLock) -> tuple[float, int]:
    stop, stats = threading.Event(), threading.Lock()
    inside = peak = 0

    def reader() -> None:
        nonlocal inside, peak
        while not stop.is_set():
            with lock.read():
                with stats:
                    inside += 1
                    peak = max(peak, inside)
                time.sleep(0.001)        # readers overlap all the time
                with stats:
                    inside -= 1

    rs = [threading.Thread(target=reader) for _ in range(8)]
    for r in rs:
        r.start()
    time.sleep(0.05)
    start = time.perf_counter()
    with lock.write():
        waited = time.perf_counter() - start
    stop.set()
    for r in rs:
        r.join()
    return waited, peak


if __name__ == "__main__":
    reads, torn = stress(RWCache())
    assert reads > 1000 and torn == 0             # many reads, none torn

    broken = RWCache()
    broken._lock = NoLock()
    assert stress(broken)[1] > 0                  # without the lock: torn reads

    waited, peak = writer_wait_and_peak_readers(ReadWriteLock())
    assert peak > 1                               # readers really share
    assert waited < 0.1                           # writer not starved

    cache, calls = RWCache(), []
    assert cache.get_or_load("x", lambda k: calls.append(k) or k.upper()) == "X"
    assert cache.get_or_load("x", lambda k: "never") == "X" and calls == ["x"]
    print("ok:", reads, "reads, 0 torn")
```

- **`_waiting_writers`** is the writer preference. Without it, eight overlapping readers keep `_readers` above zero forever and the writer never gets in. The second test checks exactly that.
- **`notify_all`, not `notify`**: readers and writers share one condition, so a single `notify()` could wake a reader that cannot proceed while the writer sleeps on.
- **Context managers** release the lock on exceptions. Never call `acquire_*` and `release_*` by hand in callers.
- **The torn-read test** pauses a writer halfway through a two-key update. With the `NoLock` control it reports torn reads every run; with the RW lock, none.

## Is it worth it under the GIL?

Rough per-read cost from a quick run (CPython 3.12, 8 threads):

| Read critical section | Plain `Lock` | `ReadWriteLock` |
|---|---|---|
| `dict.get` | about 0.2 µs | about 8× slower |
| Pure-Python computation | same | same: the GIL runs one thread anyway |
| 1 ms blocking call (GIL released) | 400 reads take 0.5 s, one at a time | about 8× faster |

- A Python RW lock costs two `Condition` round trips per read; a `Lock` is one C call. For in-memory lookups, use a plain `Lock`.
- It wins only when the read itself waits with the GIL released (disk, network, a C extension that drops the GIL), or on the free-threaded build.
- For read-mostly data, a **copy-on-write snapshot** often beats both: readers take no lock at all.

```python
import threading


class SnapshotCache:
    def __init__(self) -> None:
        self._snapshot: dict = {}
        self._write_lock = threading.Lock()   # writers still go one at a time

    def get_many(self, keys) -> list:
        snap = self._snapshot                 # one reference read, no lock
        return [snap.get(k) for k in keys]

    def put_many(self, pairs) -> None:
        with self._write_lock:
            new = dict(self._snapshot)        # O(n) copy per write
            new.update(pairs)
            self._snapshot = new              # publish in one assignment


if __name__ == "__main__":
    c = SnapshotCache()
    c.put_many([("a", 1), ("b", 2)])
    old = c._snapshot
    c.put_many([("a", 10)])
    assert c.get_many(["a", "b"]) == [10, 2] and old["a"] == 1
```

## Pitfalls

- **Not reentrant.** A thread that takes the read lock twice deadlocks if a writer starts waiting in between: the writer waits for the reader, the reader's second acquire waits for the writer.
- **No upgrade.** A thread holding the read lock that asks for the write lock waits for itself forever. Release, take the write lock, re-check.
- **Readers starve under constant writes.** In the stress test, removing the writers' `sleep(0.001)` drops reads from hundreds of thousands to single digits.
- **An LRU `get` is a write**: it moves the key to the front. A read lock there corrupts the list. See [Design an LRU cache]({{ '/lld/lru-cache/' | relative_url }}).
- **Cache stampede**: `get_or_load` can call the loader once per concurrent miss. For expensive loads, keep a per-key `Future` so one thread loads and the rest wait on it.

## Interview questions

- **Why writer preference for a cache?** Reads dominate. With reader preference, a steady stream of overlapping readers can block an update forever and serve stale data.
- **Why `while` around `wait()`?** `notify_all` wakes everyone; each thread must re-check that it can enter.
- **Would you use this in CPython for a dict cache?** No. Under the GIL a plain `Lock` is cheaper for microsecond reads. Use an RW lock when reads block on I/O, or a copy-on-write snapshot.
- **How would you make it fair?** Give each waiter a ticket number and admit in order, or alternate batches: after a writer, admit all readers waiting at that moment.
