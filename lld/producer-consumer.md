---
title: Producer-consumer
section: lld
order: 12
phase: "Phase 4 · Concurrency"
summary: Hand work between threads with queue.Queue, build the same bounded buffer from Condition, shut consumers down cleanly and apply backpressure.
---

Producers create work, consumers process it, and a buffer between them absorbs the difference in speed. The buffer is the only shared state, so if it is thread-safe, the rest of the code needs no locks. In Java this is a `BlockingQueue`; in Python it is `queue.Queue`.

**Signals in a problem statement:** "jobs arrive faster than we process them", "background worker", "pipeline", "logger that must not block requests", "N workers pulling tasks".

## With `queue.Queue`

```python
import queue
import threading

SENTINEL = object()                      # the poison pill


def producer(q: queue.Queue, start: int, count: int) -> None:
    for i in range(start, start + count):
        q.put(i)                         # blocks while the queue is full


def consumer(q: queue.Queue, results: list[int]) -> None:
    while True:
        item = q.get()                   # blocks while the queue is empty
        try:
            if item is SENTINEL:
                return
            results.append(item * item)  # list.append is safe under the GIL
        finally:
            q.task_done()                # exactly once per get(), even on error


if __name__ == "__main__":
    q: queue.Queue = queue.Queue(maxsize=10)   # bounded: producers wait
    results: list[int] = []
    consumers = [threading.Thread(target=consumer, args=(q, results)) for _ in range(3)]
    producers = [threading.Thread(target=producer, args=(q, p * 100, 100)) for p in range(4)]
    for t in consumers + producers:
        t.start()
    for t in producers:
        t.join()                         # every item is now in the queue
    q.join()                             # every item has been task_done()
    for _ in consumers:
        q.put(SENTINEL)                  # one pill per consumer
    for t in consumers:
        t.join()
    assert sorted(results) == [i * i for i in range(400)]
```

- `q.join()` blocks until every `put` has a matching `task_done()`. It is how the main thread knows the work is finished, not just dequeued.
- **Sentinel shutdown:** each consumer exits when it gets the pill, so send one per consumer. With one pill, the other consumers block forever.
- Python 3.13 adds `q.shutdown()`, which makes blocked and future `get()` calls raise `queue.ShutDown` once the queue drains.

## A bounded buffer with `Condition`

What the interviewer asks next: "Now build the queue yourself." One lock, two condition variables (Java's `wait`/`notify` on a monitor, or a `ReentrantLock` with two `Condition`s).

```python
import queue
import threading
from collections import deque


class BoundedBuffer:
    def __init__(self, capacity: int) -> None:
        self._items: deque = deque()
        self._capacity = capacity
        lock = threading.Lock()
        self._not_full = threading.Condition(lock)   # producers wait here
        self._not_empty = threading.Condition(lock)  # consumers wait here
        self.max_seen = 0

    def put(self, item) -> None:
        with self._not_full:
            while len(self._items) >= self._capacity:  # while, never if
                self._not_full.wait()
            self._items.append(item)
            self.max_seen = max(self.max_seen, len(self._items))
            self._not_empty.notify()                   # wake one consumer

    def get(self):
        with self._not_empty:
            while not self._items:
                self._not_empty.wait()
            item = self._items.popleft()
            self._not_full.notify()                    # wake one producer
            return item


def stress(buf, producers: int = 8, consumers: int = 8, per_producer: int = 2000) -> None:
    consumed: list[int] = []
    sentinel = object()

    def produce(p: int) -> None:
        for i in range(per_producer):
            buf.put(p * per_producer + i)

    def consume() -> None:
        while (item := buf.get()) is not sentinel:
            consumed.append(item)

    ps = [threading.Thread(target=produce, args=(p,)) for p in range(producers)]
    cs = [threading.Thread(target=consume) for _ in range(consumers)]
    for t in ps + cs:
        t.start()
    for t in ps:
        t.join()
    for _ in cs:
        buf.put(sentinel)
    for t in cs:
        t.join()
    total = producers * per_producer
    assert len(consumed) == total                 # nothing lost...
    assert set(consumed) == set(range(total))     # ...nothing duplicated


if __name__ == "__main__":
    for _ in range(3):
        buf = BoundedBuffer(capacity=5)
        stress(buf)
        assert buf.max_seen <= 5                  # capacity never exceeded
        stress(queue.Queue(maxsize=5))            # same contract, stdlib version
    print("stress ok")
```

**Why `while` around `wait()`:**

- Wakeups can be spurious.
- Stolen wakeups: between `notify()` and the woken thread getting the lock back, another consumer can grab the item. An `if` would then `popleft()` on an empty deque.

**`notify` vs `notify_all`:**

- With two conditions, producers only wait on `_not_full` and consumers only on `_not_empty`, so `notify()` always wakes the right kind of thread. One wakeup per item, no thundering herd.
- With a single condition for both, `notify()` can wake a producer when a consumer was needed. That producer goes back to sleep, the wakeup is lost, and with enough bad luck every thread waits forever. Use `notify_all()` in that design.

## Backpressure

A bounded buffer makes a slow consumer slow the producer down instead of letting memory grow. An unbounded `Queue()` never pushes back: the problem shows up as rising memory and stale work, not as errors.

| Strategy | Python | Use when |
|---|---|---|
| Block the producer | `q.put(item)` | Internal pipelines, batch jobs |
| Block with a deadline, then fail | `q.put(item, timeout=0.5)` raises `queue.Full` | Request paths: return 429/503, don't hang |
| Drop | `q.put_nowait(item)`, catch `queue.Full`, count drops | Metrics, debug logs, sampling |

> **Rule of thumb:** size the queue to absorb a burst (a few seconds of peak input), not to hide a consumer that is slower on average. No queue size fixes that.

## When to use, when not

- **Use:** decoupling a fast producer from slow I/O (log shipping, email sends), fixed worker pools, multi-stage pipelines.
- **Not for durable work:** an in-memory queue loses everything on restart. Use a Postgres task table, Celery, SQS or Kafka.
- **Just need N calls in parallel?** `ThreadPoolExecutor` is a producer-consumer queue already. See [Thread pools and executors]({{ '/lld/thread-pools-and-executors/' | relative_url }}).

## Pitfalls

- Missing `task_done()` makes `join()` hang forever; an extra call raises `ValueError`. Put it in `finally`.
- An exception in a consumer kills that thread. With every consumer dead, producers block forever on a full queue. Catch per item, log, keep going.
- `if not q.empty(): q.get()` is check-then-act. Another consumer can win. Use `get(timeout=...)` and catch `queue.Empty`.
- Processing the item while holding the buffer lock. Take the item, release, then process.
- Daemon consumers are killed at exit with items still queued. Shut down with pills and `join()`.

## Interview questions

- **Why two condition variables?** So `notify()` wakes a thread that can make progress: producers wait for space, consumers for items.
- **How do you stop consumers cleanly?** One sentinel per consumer after producers finish, or `Queue.shutdown()` on 3.13+. Then `join()` the threads.
- **What is backpressure here?** `put()` blocking on a full bounded queue. Producers slow to the consumers' rate instead of growing memory.
- **How does this map to a distributed system?** The queue becomes Kafka or SQS, `task_done()` becomes an ack, and a crashed consumer's item is redelivered after a visibility timeout, so consumers must be idempotent.
