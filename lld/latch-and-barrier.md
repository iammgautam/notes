---
title: Latches and barriers
section: lld
order: 16
phase: "Phase 4 · Concurrency"
summary: CountDownLatch built from a Condition, threading.Barrier vs Java's CyclicBarrier, broken barriers, and when you need neither.
---

Both make threads wait for each other. A **latch** is a one-shot gate: it opens when a count reaches zero and stays open. A **barrier** makes a fixed group of threads wait until all of them arrive, then releases them together, and can be reused for the next round.

## Which one do you need

| Need | Python | Java name |
|---|---|---|
| Wait until N things have happened, once | `CountDownLatch` (below) | `CountDownLatch` |
| One "go" signal for many threads | `threading.Event` (a latch of 1) | `CountDownLatch(1)` |
| N threads meet, repeatedly, between phases | `threading.Barrier(N)` | `CyclicBarrier` |
| Wait for N tasks you submitted | `concurrent.futures.wait(futures)` | `invokeAll` |
| Wait for threads you started | `t.join()` | `join()` |

> **Rule of thumb:** If you own the threads or futures, `join()` or `futures.wait()` is simpler than a latch. A latch earns its place when the waiter doesn't know who will count down, or the events aren't thread exits ("all 8 workers have loaded their model").

## CountDownLatch from scratch

The Java names are `countDown()` and `await()`. `await` is a keyword in Python, so the method is `wait`.

```python
import threading


class CountDownLatch:
    def __init__(self, count: int):
        if count < 0:
            raise ValueError("count must be >= 0")
        self._count = count
        self._cond = threading.Condition()

    def count_down(self) -> None:
        with self._cond:
            if self._count == 0:
                return                       # extra calls are no-ops
            self._count -= 1
            if self._count == 0:
                self._cond.notify_all()      # wake every waiter, once

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the count hits 0. False if the timeout ran out."""
        with self._cond:
            return self._cond.wait_for(lambda: self._count == 0, timeout)

    @property
    def count(self) -> int:
        with self._cond:
            return self._count


if __name__ == "__main__":
    import random
    import time

    for _ in range(20):                      # repeat to shake out races
        n = 8
        ready, start, done = (CountDownLatch(n), CountDownLatch(1),
                              CountDownLatch(n))
        results = []

        def worker(i):
            ready.count_down()               # "I'm warmed up"
            start.wait()                     # starting gate
            time.sleep(random.random() / 1000)
            results.append(i)
            done.count_down()

        for i in range(n):
            threading.Thread(target=worker, args=(i,)).start()
        assert ready.wait(timeout=2)         # all N workers ready
        assert results == []                 # nobody passed the gate yet
        start.count_down()                   # release them together
        assert done.wait(timeout=2)
        assert sorted(results) == list(range(n))

    never = CountDownLatch(1)
    assert never.wait(timeout=0.05) is False and never.count == 1
    never.count_down()
    never.count_down()
    assert never.count == 0 and never.wait(timeout=0)
    print("ok")
```

What makes it correct:

- `wait_for` re-checks the predicate in a loop, so spurious wakeups and late arrivals (count already 0) both work.
- `notify_all`, not `notify`: every waiter must wake.
- Every read and write of `_count` is under the condition's lock. The GIL does not make `-= 1` atomic.

## Barriers: `threading.Barrier` vs `CyclicBarrier`

| Feature | `threading.Barrier` | Java `CyclicBarrier` |
|---|---|---|
| Parties | `Barrier(parties)` | `new CyclicBarrier(parties)` |
| Reusable after it trips | Yes | Yes |
| Action when it trips | `action=` runs in one of the threads | `barrierAction` runs in the last thread to arrive |
| Return value | `wait()` returns a distinct 0..N-1 | `await()` returns arrival index |
| Timeout | `Barrier(..., timeout=)` or `wait(timeout)` | `await(timeout, unit)` |
| Broken state | `BrokenBarrierError` in every waiter | `BrokenBarrierException` |

A barrier **breaks** when a waiter times out, someone calls `abort()`, `reset()` is called while threads wait, or the action raises. Every current and future `wait()` then raises until `reset()`. That's the point: if one party dies, the others fail fast instead of hanging forever.

### Phased computation

Each thread updates its own slot, then all meet; the action publishes a combined result once per phase. A **second** barrier stops a fast thread from starting the next phase's writes while slow threads are still reading this phase's result.

```python
import threading

N = 4
data = [1, 2, 3, 4]                         # sum 10
totals: list[int] = []
seen: list[tuple[int, int]] = []            # (phase, total a thread read)

computed = threading.Barrier(N, action=lambda: totals.append(sum(data)))
read_done = threading.Barrier(N)


def worker(i: int) -> None:
    for phase in range(3):
        data[i] *= 2                        # write: only my slot
        computed.wait()                     # all writes in; action ran once
        seen.append((phase, totals[-1]))    # read the shared result
        read_done.wait()                    # no writes until all have read


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert totals == [20, 40, 80]
assert sorted(seen) == [(p, 20 * 2**p) for p in range(3) for _ in range(N)]

# Broken barrier: the third party never arrives, so both waiters time out.
gate = threading.Barrier(3, timeout=0.05)
errors: list[str] = []


def waiter() -> None:
    try:
        gate.wait()
    except threading.BrokenBarrierError:
        errors.append("broken")


ts = [threading.Thread(target=waiter) for _ in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join()
assert errors == ["broken", "broken"] and gate.broken
gate.reset()                                # usable again
assert not gate.broken
print("ok")
```

### Cyclic barrier from scratch

The interview question behind "implement CyclicBarrier" is: how does a waiter tell *its* round tripping from the next round starting? Use a generation counter. A plain `count == 0` check fails because the last thread resets the count for the next round before the sleepers wake up.

```python
import threading


class CyclicBarrier:
    def __init__(self, parties: int):
        self._parties = parties
        self._waiting = 0
        self._generation = 0
        self._cond = threading.Condition()

    def wait(self) -> int:
        with self._cond:
            gen, index = self._generation, self._waiting
            self._waiting += 1
            if self._waiting == self._parties:  # last arrival trips it
                self._waiting = 0
                self._generation += 1
                self._cond.notify_all()
                return index
            self._cond.wait_for(lambda: self._generation != gen)
            return index


if __name__ == "__main__":
    n, rounds = 5, 200
    barrier = CyclicBarrier(n)
    arrived = [0] * rounds
    lock = threading.Lock()

    def worker():
        for r in range(rounds):
            with lock:
                arrived[r] += 1
            barrier.wait()
            assert arrived[r] == n          # nobody left before all arrived

    ts = [threading.Thread(target=worker) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts) and arrived == [n] * rounds
    print("ok")
```

Timeouts and the broken state are left out; `threading.Barrier` has them, so use it outside the interview.

## Use cases

- **Start gate:** load N workers, wait for "ready" from each (latch of N), then release them at once (latch of 1 or `Event`). Load tests and benchmarks use this so threads start together.
- **Wait for N services:** a process doesn't accept traffic until its cache, DB pool and config watcher have all signalled.
- **Phased computation:** simulations, iterative algorithms, parallel matrix steps: compute, sync, combine, repeat.
- **Tests:** force an interleaving (both threads hold their first lock) to reproduce a race or a deadlock on demand.

## Pitfalls

- **Count too high:** a worker that crashes before `count_down()` leaves the waiter stuck. Count down in `finally`, and always wait with a timeout.
- **Reusing a latch:** it's one-shot. For rounds, use a barrier.
- **Barrier with a thread pool smaller than the parties:** with 4 pool threads and `Barrier(5)`, the fifth task never runs because the four are blocked. Parties must be at most the number of threads that can run at once.
- **Forgetting broken barriers:** catch `BrokenBarrierError` and decide whether to `reset()` and retry or give up.

## Interview questions

- **Latch vs barrier?** Latch: one-shot, threads that count down don't wait, waiters are a different group. Barrier: reusable, the same N threads both arrive and wait.
- **Why `notify_all` in the latch?** Several threads may be waiting; `notify` would wake one and leave the rest asleep forever.
- **Why a generation counter in a cyclic barrier?** The count is reset for the next round as soon as the last thread arrives, so sleepers can't check it. The generation changes exactly once per trip.
- **What breaks a `threading.Barrier`?** A timeout, `abort()`, `reset()` while threads wait, or an exception in the action. All waiters get `BrokenBarrierError`.

Related: [Python concurrency primitives]({{ '/lld/python-concurrency-primitives/' | relative_url }}), [thread pools and executors]({{ '/lld/thread-pools-and-executors/' | relative_url }}).
