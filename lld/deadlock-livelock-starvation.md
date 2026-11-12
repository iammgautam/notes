---
title: Deadlock, livelock and starvation
section: lld
order: 17
phase: "Phase 4 · Concurrency"
summary: The four Coffman conditions, a reproducible two-lock deadlock and its three fixes, dining philosophers, livelock, starvation and DB deadlocks.
---

Three ways threads stop making progress:

| Failure | What you see | CPU |
|---|---|---|
| **Deadlock** | Threads wait on each other forever | Idle |
| **Livelock** | Threads keep reacting to each other, nobody finishes | Busy |
| **Starvation** | Most threads progress, one never gets its turn | Busy |

## Coffman conditions

A deadlock needs all four. Break any one and it can't happen.

| Condition | Meaning | How to break it |
|---|---|---|
| Mutual exclusion | A resource has one holder at a time | Share it (immutable data, read-only copies) |
| Hold and wait | Hold one lock while waiting for another | Take all locks at once, or only one lock |
| No preemption | A lock can't be taken away | Try-lock with timeout: give up and release |
| Circular wait | A waits on B, B waits on A | **Global lock order** (the usual fix) |

## A reproducible deadlock

Two threads take the same two locks in opposite order. A barrier forces the bad interleaving every run, and the timeout on the second acquire lets the demo finish instead of hanging.

```python
import threading

lock_a, lock_b = threading.Lock(), threading.Lock()
sync = threading.Barrier(2)               # forces the bad interleaving
outcome: dict[str, str] = {}


def worker(name: str, first: threading.Lock, second: threading.Lock) -> None:
    with first:
        sync.wait()                       # both threads now hold one lock
        got = second.acquire(timeout=0.2) # plain acquire() hangs forever
        outcome[name] = "ok" if got else "deadlock"
        if got:
            second.release()
        sync.wait()                       # hold until both have tried


t1 = threading.Thread(target=worker, args=("t1", lock_a, lock_b))
t2 = threading.Thread(target=worker, args=("t2", lock_b, lock_a))  # reversed
for t in (t1, t2):
    t.start()
for t in (t1, t2):
    t.join()
assert outcome == {"t1": "deadlock", "t2": "deadlock"}
print("ok")
```

## Three fixes

1. **Global lock order** (breaks circular wait): always lock the lower id first.
2. **Try-lock with timeout and back-off** (breaks no-preemption): if the second lock isn't free, release the first, sleep a random moment, retry. Java calls this `tryLock(timeout)`.
3. **One lock** (breaks hold-and-wait): a single lock around the whole operation. Simplest, and it serializes everything.

```python
import random
import threading
import time


class Account:
    def __init__(self, id_: int, balance: int):
        self.id, self.balance = id_, balance
        self.lock = threading.Lock()


def transfer_ordered(src: Account, dst: Account, amount: int) -> None:
    first, second = sorted((src, dst), key=lambda a: a.id)  # one global order
    with first.lock, second.lock:
        src.balance -= amount
        dst.balance += amount


def transfer_backoff(src: Account, dst: Account, amount: int) -> None:
    while True:
        with src.lock:
            if dst.lock.acquire(timeout=0.001):       # try-lock
                try:
                    src.balance -= amount
                    dst.balance += amount
                    return
                finally:
                    dst.lock.release()
        time.sleep(random.uniform(0, 0.001))  # random back-off: no lockstep


if __name__ == "__main__":
    for transfer in (transfer_ordered, transfer_backoff):
        a, b = Account(1, 1_000), Account(2, 1_000)

        def run(src, dst):
            for _ in range(2_000):
                transfer(src, dst, 1)

        ts = [threading.Thread(target=run, args=p) for p in [(a, b), (b, a)] * 4]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in ts), f"{transfer.__name__} hung"
        assert a.balance == b.balance == 1_000   # equal flows both ways
    print("ok")
```

> **Interview tip:** Lead with lock ordering. Mention try-lock as the fallback when you can't know all locks up front (the second lock depends on data read under the first).

## Dining philosophers

Five philosophers, five forks, each needs both neighbours' forks. If everyone picks up the left fork first, all hold one and wait for the other: circular wait. Two correct fixes:

- **Resource ordering:** pick up the lower-numbered fork first. Philosopher 4 reaches for fork 0 before fork 4, so the cycle can't close.
- **Waiter:** a `Semaphore(n - 1)` lets at most four sit at once, so at least one can always get both forks.

```python
import threading


def dine(n: int = 5, meals: int = 1_000, use_waiter: bool = False) -> list[int]:
    forks = [threading.Lock() for _ in range(n)]
    waiter = threading.Semaphore(n - 1 if use_waiter else n)
    eaten = [0] * n

    def philosopher(i: int) -> None:
        a, b = i, (i + 1) % n               # left fork, right fork
        if not use_waiter:
            a, b = min(a, b), max(a, b)     # fix 1: lower-numbered fork first
        for _ in range(meals):
            with waiter:                    # fix 2: at most n-1 at the table
                with forks[a], forks[b]:
                    eaten[i] += 1

    threads = [threading.Thread(target=philosopher, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "deadlock"
    return eaten


if __name__ == "__main__":
    assert dine() == [1_000] * 5
    assert dine(use_waiter=True) == [1_000] * 5
    print("ok")
```

The `join(timeout=...)` plus `is_alive()` check is how you test for deadlock: a hung test fails with a message instead of freezing CI.

## Livelock

Two threads each hold one lock, fail a try-lock on the other, release, and retry at the same instant, forever: two people stepping aside in a corridor, both to the same side. Replace the random sleep in `transfer_backoff` with `time.sleep(0.001)` (or no sleep) and the two opposite transfers can fall into exactly that lockstep. The fix is the jitter already there: random back-off breaks the symmetry, and exponential back-off with jitter (the same idea as retrying HTTP calls) caps the wasted work under heavy contention.

## Starvation and fairness

| Cause | Example | Fix |
|---|---|---|
| Unfair lock | `threading.Lock` makes no FIFO promise; a thread that just released can grab it again | FIFO handoff via `queue.Queue`, or a ticket lock built on `Condition` |
| Reader-preferring RW lock | A steady stream of readers keeps the writer out forever | Writer preference: block new readers once a writer waits |
| Strict priority | A low-priority job never runs while high-priority work keeps arriving | Aging: raise priority the longer a job waits |
| Long critical sections | One thread holds the lock for seconds | Shrink the section; move I/O out |

Java's `ReentrantLock(true)` is the "fair" mode: FIFO order, lower throughput. Fairness always costs throughput, so ask for it only when someone is actually starving. See [read-write lock cache]({{ '/lld/read-write-lock-cache/' | relative_url }}) for writer starvation.

## Deadlocks in the database

- PostgreSQL detects them: after `deadlock_timeout` (1s by default) it checks the wait graph and aborts one transaction with `deadlock detected` (SQLSTATE `40P01`).
- Typical cause: two transactions update the same rows in different orders (transfer A→B and B→A, or bulk updates without `ORDER BY`).
- Fix it the same way: lock rows in a fixed order, keep transactions short, and **retry** the aborted transaction (it's safe because it rolled back).

```sql
BEGIN;
SELECT id FROM accounts WHERE id IN (7, 3) ORDER BY id FOR UPDATE;
UPDATE accounts SET balance = balance - 100 WHERE id = 7;
UPDATE accounts SET balance = balance + 100 WHERE id = 3;
COMMIT;
```

- `SET lock_timeout = '2s'` turns a long wait into an error you can handle, the DB version of a try-lock.

## Pitfalls

- `transfer(a, a)` with lock ordering takes the same `Lock` twice and deadlocks itself. Reject `src is dst` first.
- Calling a callback or observer while holding a lock: it may take another lock in the reverse order.
- A thread waiting on a future that needs a worker from the same full pool (pool self-deadlock).
- `RLock` hides re-entrancy bugs but doesn't fix lock-order bugs.

## Interview questions

- **Deadlock vs livelock?** Deadlock: blocked, no CPU. Livelock: running, retrying, no progress.
- **How do you prevent deadlock with many locks?** A global order (sort by id) and acquire in that order; document it next to the locks.
- **How do you detect one in a running Python process?** `faulthandler.dump_traceback_later()` or `py-spy dump` shows every thread's stack; two threads parked in `acquire` on each other's locks is the signature.
- **Does the GIL prevent deadlocks?** No. It's released while a thread waits on a lock.

Related: [Python concurrency primitives]({{ '/lld/python-concurrency-primitives/' | relative_url }}), [latches and barriers]({{ '/lld/latch-and-barrier/' | relative_url }}).
