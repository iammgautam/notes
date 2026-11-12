---
title: Python concurrency primitives
section: lld
order: 10
phase: "Phase 4 · Concurrency"
summary: Threads vs processes, what the GIL does and does not protect, every threading primitive with its Java name, and a race that really loses updates.
---

Interviewers often use Java's concurrency vocabulary. This note maps each Java term to Python, shows why the GIL does not make your code thread-safe, and runs every primitive the next notes use.

## Threads, processes and the GIL

| Tool | Runs in parallel? | Use for |
|---|---|---|
| `threading` | No for Python bytecode, yes while blocked on I/O | I/O-bound work, shared in-memory state |
| `multiprocessing` / `ProcessPoolExecutor` | Yes, separate interpreters | CPU-bound work |
| `asyncio` | One thread, cooperative | Many concurrent network calls |

The **GIL** (global interpreter lock) lets one thread run Python bytecode at a time.

- **It protects** the interpreter's own data: reference counts, and the internals of a single `list.append`, `dict[k] = v` or `deque.popleft`. Those cannot corrupt the container.
- **It does not protect** your logic. The interpreter can switch threads between any two bytecodes, and `self.n += 1` is several:

```text
LOAD_ATTR   n        # read
LOAD_CONST  1
BINARY_OP   +=       # compute
STORE_ATTR  n        # write  <- another thread may have written in between
```

- Python 3.13 adds an optional free-threaded build (PEP 703) with no GIL. Code that locks correctly keeps working. Code that leaned on the GIL by accident breaks.

## A race that loses updates

`time.sleep(0)` releases the GIL between the read and the write, so the lost update shows up every run instead of once in a while.

```python
import threading
import time


class UnsafeCounter:
    def __init__(self) -> None:
        self.value = 0

    def increment(self) -> None:
        current = self.value      # read
        time.sleep(0)             # yield: another thread reads the same value
        self.value = current + 1  # write back a stale result


class SafeCounter(UnsafeCounter):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def increment(self) -> None:
        with self._lock:          # read-modify-write is now one critical section
            super().increment()


def hammer(counter: UnsafeCounter, threads: int = 8, per_thread: int = 1000) -> int:
    def work() -> None:
        for _ in range(per_thread):
            counter.increment()

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return counter.value


if __name__ == "__main__":
    lost = hammer(UnsafeCounter())
    assert lost < 8000, lost          # typically around 1,000-1,500
    assert hammer(SafeCounter()) == 8000
    print("unsafe:", lost, "safe: 8000")
```

## The primitives

One runnable tour. Each comment names the Java equivalent.

```python
import queue
import threading
import time

rlock = threading.RLock()            # ReentrantLock: owner can re-acquire


def outer() -> str:
    with rlock:
        return inner()               # a plain Lock would deadlock here


def inner() -> str:
    with rlock:
        return "ok"


cond = threading.Condition()         # wait/notify/notifyAll on a monitor
items: list[int] = []


def take() -> int:
    with cond:
        cond.wait_for(lambda: items)  # loops on the predicate, like while(!x) wait()
        return items.pop()


def give(x: int) -> None:
    with cond:
        items.append(x)
        cond.notify()                # notify_all() wakes every waiter


slots = threading.BoundedSemaphore(2)  # Semaphore; raises if released too often
inside = peak = 0
stats = threading.Lock()


def limited() -> None:
    global inside, peak
    with slots:
        with stats:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.01)
        with stats:
            inside -= 1


go = threading.Event()               # a flag threads can wait on (volatile + latch)
barrier = threading.Barrier(3)       # CyclicBarrier
ctx = threading.local()              # ThreadLocal
out: queue.Queue = queue.Queue()     # BlockingQueue


def stage(name: str) -> None:
    ctx.name = name                  # other threads never see this value
    go.wait()
    barrier.wait()                   # all three arrive before any continues
    out.put(ctx.name)


def run_all(target, args_list) -> None:
    ts = [threading.Thread(target=target, args=a) for a in args_list]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


if __name__ == "__main__":
    assert outer() == "ok"

    t = threading.Thread(target=lambda: out.put(take()))
    t.start()
    give(42)
    t.join()
    assert out.get() == 42

    run_all(limited, [()] * 6)
    assert peak == 2

    threading.Timer(0.05, go.set).start()
    run_all(stage, [("a",), ("b",), ("c",)])
    assert sorted(out.get() for _ in range(3)) == ["a", "b", "c"]
    print("primitives ok")
```

- `Lock.acquire(timeout=1.0)` is `tryLock(1, SECONDS)`; it returns `False` instead of raising.
- `Condition` always needs a loop around `wait()`: wakeups can be spurious, and another thread can consume the state first. `wait_for(predicate)` writes the loop for you.
- `Semaphore` lets `release()` push the count above its start value; `BoundedSemaphore` raises `ValueError`, which catches bugs.
- `queue.Queue(maxsize=n)` blocks `put` when full and `get` when empty. See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

## Atomics, volatile and synchronized

- **`synchronized`**: no keyword and no hidden per-object monitor. Create `self._lock = threading.Lock()` and wrap the body in `with self._lock:`. `with` releases on exceptions, like a Java block.
- **`volatile`**: none. Under the GIL every thread sees the latest attribute value, so a plain bool stop flag works. Prefer `threading.Event`: same intent, plus `wait(timeout)` so the worker sleeps instead of spinning.
- **`AtomicInteger`**: none. Write a small class with a lock. `itertools.count()` works as an ID generator because `next()` runs in C while holding the GIL, but that is a CPython detail, not a documented guarantee, and you cannot read the current value without consuming it.

```python
import itertools
import threading


class AtomicInteger:
    def __init__(self, value: int = 0) -> None:
        self._value = value
        self._lock = threading.Lock()

    def increment_and_get(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    def compare_and_set(self, expected: int, new: int) -> bool:
        with self._lock:
            if self._value != expected:
                return False
            self._value = new
            return True

    def get(self) -> int:
        return self._value           # a single attribute read is safe


if __name__ == "__main__":
    n, ids, seen = AtomicInteger(), itertools.count(1), []

    def work() -> None:
        for _ in range(10_000):
            n.increment_and_get()
            seen.append(next(ids))   # list.append is safe; GIL protects it

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert n.get() == 80_000
    assert len(set(seen)) == 80_000  # no duplicate IDs
    assert n.compare_and_set(80_000, 0) and not n.compare_and_set(5, 1)
```

## Java to Python quick reference

| Java | Python |
|---|---|
| `synchronized` method or block | `with self._lock:` |
| `volatile boolean` flag | `threading.Event` |
| `ReentrantLock` | `threading.RLock` |
| `lock.tryLock(t)` | `lock.acquire(timeout=t)` |
| `wait` / `notify` / `notifyAll` | `Condition.wait` / `notify` / `notify_all` |
| `Semaphore` | `Semaphore`, `BoundedSemaphore` |
| `CountDownLatch` | `Event`, or a counter plus `Condition` |
| `CyclicBarrier` | `threading.Barrier` |
| `ThreadLocal` | `threading.local` |
| `BlockingQueue` | `queue.Queue(maxsize=n)` |
| `AtomicInteger` | int plus `Lock`; `itertools.count` for IDs |
| `ConcurrentHashMap` | `dict` plus `Lock` for compound ops |
| `ReadWriteLock` | not in stdlib; [build one]({{ '/lld/read-write-lock-cache/' | relative_url }}) |
| `ExecutorService` | `concurrent.futures.ThreadPoolExecutor` |
| `CompletableFuture` | `Future.add_done_callback`, `asyncio` |

## Pitfalls

- Locking the write but not the read that decided it (check-then-act).
- Using `Lock` where a method calls another locked method on the same object: deadlock. Use `RLock` or split into locked public and unlocked private methods.
- `if q.empty(): q.get()` is a race. Call `get(timeout=...)` and handle `queue.Empty`.
- An uncaught exception kills only that thread and prints a traceback; the main thread carries on. Set `threading.excepthook` or catch inside the target.

## Interview questions

- **Does the GIL make `count += 1` thread-safe?** No. It is a read, an add and a write as separate bytecodes; a switch in between loses an update.
- **Lock vs RLock?** `RLock` tracks the owner and a count so the same thread can re-enter. It is slower and can hide confused lock ownership, so default to `Lock`.
- **Why `while` around `Condition.wait()`?** Spurious wakeups, and another thread may take the item between `notify` and your re-acquire. Re-check the predicate.
- **Threads or processes for CPU-bound work?** Processes. Threads take turns on the GIL, so CPU-bound threads give no speedup.
- **What replaces `volatile`?** Nothing is needed for visibility in CPython; use `Event` for flags and a `Lock` for anything compound.
