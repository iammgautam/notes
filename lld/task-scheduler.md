---
title: Design a task scheduler
section: lld
order: 27
phase: "Phase 5 · Intermediate"
summary: Priority and delayed tasks on two heaps, recurring jobs, retries with exponential backoff, worker threads on a Condition, cancellation and graceful shutdown.
---

## Requirements

Clarifying questions to ask first:

- One-off tasks only, or also delayed and recurring ones? Cron expressions?
- What does priority mean when a high-priority task is due later than a low one?
- On failure: retry how many times, with what backoff?
- Can a running task be cancelled, or only a pending one?
- On shutdown: finish queued work, drop it, or persist it?
- In-process only, or must tasks survive a restart?

Functional:

- `submit(fn, priority, delay, interval, max_retries) -> Task`.
- Among tasks that are due, lower `priority` runs first; ties run in submit order.
- Delayed tasks wait until `run_at`; recurring tasks run again `interval` seconds after each run ends.
- Failures retry with exponential backoff and jitter, up to `max_retries`, then the task is `FAILED`.
- `cancel(task)`: a pending task never runs; a running task finishes but is not rescheduled.
- `shutdown()`: reject new tasks, cancel future (delayed) work, finish due work, join the workers.

Non-functional: N worker threads; idle workers sleep (no busy polling) and wake exactly when the next task is due; a failing task never kills a worker.

Out of scope: persistence, cron syntax, distributed workers, killing a running task (all follow-ups).

## Entities and relationships

```text
+--------------------------------------+         +----------------------+
| Scheduler                            | 1     * | Task                 |
| _delayed: heap (run_at, seq, task)   |-------->| id, fn, priority     |
| _ready:   heap (priority, seq, task) |  holds  | interval, max_retries|
| _cv: Condition (guards both heaps)   |         | run_at, attempts     |
| _workers: list[Thread]               |         | runs, status, error  |
| base_backoff, _stopping              |         | cancelled            |
| submit, cancel, shutdown             |         | done: Event          |
| _next(), _run(task), _schedule()     |         +----------+-----------+
+--------------------------------------+                    | has
                                                            v
                                          Status (Enum): PENDING, RUNNING,
                                          DONE, FAILED, CANCELLED

worker thread loop:  with _cv: task = _next()  ->  _run(task) outside lock
```

Why two heaps: one heap ordered by `(run_at, priority)` would run a low-priority task due at `t=4.9` before a high-priority task due at `t=5.0`, even when both are overdue. Keeping future tasks by time and due tasks by priority gives the right order in both cases.

## Patterns and why

| Pattern | Where | Why (what it prevents) |
|---------|-------|------------------------|
| Command | `Task` wraps `fn` plus its metadata | The scheduler knowing anything about what a task does |
| Producer-consumer | `submit` produces, workers consume | Callers blocking on execution; unbounded thread creation |
| Monitor (Condition) | `_cv` guards heaps; `wait(timeout)` sleeps | Busy polling, and missed wake-ups when an earlier task arrives |
| Lazy deletion | Cancelled tasks stay in a heap, skipped on pop | `O(n)` removal from the middle of a heap |
| Strategy (extension) | Trigger: once, fixed delay, cron | Hard-coding recurrence rules into the worker loop |

## Code

```python
import heapq
import itertools
import random
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class Status(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(eq=False)
class Task:
    id: int
    fn: Callable[[], object]
    priority: int = 5                    # lower runs first
    interval: float | None = None        # recurring: seconds after each run ends
    max_retries: int = 0
    run_at: float = 0.0
    attempts: int = 0                    # consecutive failures
    runs: int = 0
    status: Status = Status.PENDING
    cancelled: bool = False
    error: Exception | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)


class Scheduler:
    def __init__(self, workers: int = 4, base_backoff: float = 0.5):
        self.base_backoff = base_backoff
        self._delayed: list[tuple[float, int, Task]] = []   # by run_at
        self._ready: list[tuple[int, int, Task]] = []       # by priority
        self._seq = itertools.count()                        # FIFO tie-break
        self._ids = itertools.count(1)
        self._cv = threading.Condition()                     # guards all of the above
        self._stopping = False
        self._workers = [threading.Thread(target=self._loop, daemon=True)
                         for _ in range(workers)]
        for w in self._workers:
            w.start()

    def submit(self, fn: Callable[[], object], *, priority: int = 5,
               delay: float = 0.0, interval: float | None = None,
               max_retries: int = 0) -> Task:
        with self._cv:
            if self._stopping:
                raise RuntimeError("scheduler is shut down")
            t = Task(next(self._ids), fn, priority, interval, max_retries)
            self._schedule(t, time.monotonic() + delay)
            return t

    def cancel(self, t: Task) -> bool:
        with self._cv:
            if t.done.is_set():
                return False
            t.cancelled = True           # pending: skipped on pop; running: no rerun
            if t.status is Status.PENDING:
                self._finish(t, Status.CANCELLED)
            return True

    def shutdown(self, cancel_pending: bool = False) -> None:
        """Stop intake, drop future work, let due work finish, join workers."""
        with self._cv:
            self._stopping = True
            self._promote(time.monotonic())     # due now counts as "queued"
            dropped = self._delayed + (self._ready if cancel_pending else [])
            for *_, t in dropped:
                t.cancelled = True
                self._finish(t, Status.CANCELLED)
            self._delayed.clear()
            if cancel_pending:
                self._ready.clear()
            self._cv.notify_all()
        for w in self._workers:
            w.join()

    # ---- internals: everything below except _loop/_run needs _cv held ----
    def _schedule(self, t: Task, run_at: float) -> None:
        t.run_at, t.status = run_at, Status.PENDING
        heapq.heappush(self._delayed, (run_at, next(self._seq), t))
        self._cv.notify()                # a sleeper may need a shorter timeout

    def _finish(self, t: Task, status: Status) -> None:
        t.status = status
        t.done.set()

    def _promote(self, now: float) -> None:
        while self._delayed and self._delayed[0][0] <= now:
            _, _, t = heapq.heappop(self._delayed)
            heapq.heappush(self._ready, (t.priority, next(self._seq), t))

    def _next(self) -> Task | None:
        while True:
            now = time.monotonic()
            self._promote(now)
            while self._ready:
                _, _, t = heapq.heappop(self._ready)
                if not t.cancelled:      # lazy deletion
                    t.status = Status.RUNNING
                    return t
            if self._stopping:
                return None
            timeout = self._delayed[0][0] - now if self._delayed else None
            self._cv.wait(timeout)       # releases the lock while asleep

    def _loop(self) -> None:
        while True:
            with self._cv:
                t = self._next()
            if t is None:
                return
            self._run(t)

    def _run(self, t: Task) -> None:
        try:
            t.fn()                       # outside the lock: other workers keep going
            err = None
        except Exception as e:           # one bad task must not kill the worker
            err = e
        with self._cv:
            t.runs += 1
            if err is None:
                t.attempts, again, delay = 0, t.interval is not None, t.interval
            else:
                t.attempts, t.error = t.attempts + 1, err
                again = t.attempts <= t.max_retries
                delay = (self.base_backoff * 2 ** (t.attempts - 1)
                         * random.uniform(0.5, 1.0))           # backoff + jitter
            if t.cancelled:
                self._finish(t, Status.CANCELLED)
            elif again and not self._stopping:
                self._schedule(t, time.monotonic() + delay)
            else:
                self._finish(t, Status.DONE if err is None else Status.FAILED)


if __name__ == "__main__":
    # 1. priority among due tasks (one worker, so the order is deterministic)
    s = Scheduler(workers=1)
    gate, started, order = threading.Event(), threading.Event(), []
    s.submit(lambda: (started.set(), gate.wait()))   # occupy the only worker
    started.wait()
    for p in (9, 1, 5):
        s.submit(lambda p=p: order.append(p), priority=p)
    gate.set()
    s.shutdown()                                     # finishes due work, joins
    assert order == [1, 5, 9], order

    # 2. retries with backoff, delays, recurring, cancellation
    s = Scheduler(workers=4, base_backoff=0.01)
    calls = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("transient")

    ok = s.submit(flaky, max_retries=3)
    dead = s.submit(lambda: 1 / 0, max_retries=2)
    later = s.submit(lambda: None, delay=0.05)
    never = s.submit(lambda: None, delay=60)
    ticks: list[int] = []
    tick = s.submit(lambda: ticks.append(1), interval=0.01)
    assert s.cancel(never) and never.status is Status.CANCELLED
    for t in (ok, dead, later):
        assert t.done.wait(2)
    assert ok.status is Status.DONE and len(calls) == 3
    assert dead.status is Status.FAILED and dead.runs == 3
    assert isinstance(dead.error, ZeroDivisionError)
    time.sleep(0.1)
    assert s.cancel(tick) and tick.done.wait(1) and len(ticks) >= 3
    s.shutdown()

    # 3. graceful shutdown: future work cancelled, new work rejected
    s = Scheduler(workers=2)
    future = s.submit(lambda: None, delay=60)
    s.shutdown()
    assert future.status is Status.CANCELLED
    try:
        s.submit(lambda: None)
        raise AssertionError("accepted work after shutdown")
    except RuntimeError:
        pass

    # 4. stress: 8 workers, 2,000 tasks with random priorities, each runs once
    sys.setswitchinterval(1e-6)                      # force frequent switches
    s, seen = Scheduler(workers=8), []
    for i in range(2000):
        s.submit(lambda i=i: seen.append(i), priority=random.randint(0, 9))
    s.shutdown()
    assert sorted(seen) == list(range(2000))
    print("ok")
```

`concurrent.futures.ThreadPoolExecutor` (Java's `ExecutorService`) runs tasks but has no priorities or delays, which is why the workers here pull from their own heaps. Java's `ScheduledExecutorService` is the closest built-in equivalent; see [Thread pools and executors]({{ '/lld/thread-pools-and-executors/' | relative_url }}).

## Key flows

A delayed task becoming due while a worker sleeps:

```text
client      Scheduler (_cv)        _delayed        _ready       worker
  |              |                     |              |   wait(timeout)
  | submit(delay=0.05)                 |              |   (lock released)
  |------------->| lock                |              |            |
  |              |--push(run_at)------>|              |            |
  |              |--notify()------------------------------------>| wakes
  |<-- Task -----| unlock              |              |            |
  |              |  worker: _promote(now): nothing due yet         |
  |              |  wait(run_at - now) ............................|
  |              |  timeout fires: _promote moves task ->|         |
  |              |  pop lowest priority <----------------|         |
  |              |  status = RUNNING, unlock                       |
  |              |                     |              |   fn() runs|
```

A failing task with retries:

```text
worker               Task                       Scheduler (_cv)
  | fn() raises        |                              |
  |--attempts += 1---->|                              |
  | attempts <= max?   |                              |
  |  yes: delay = base * 2^(attempts-1) * jitter      |
  |----------------------------- _schedule(now+delay)>| push _delayed
  |  no:  status = FAILED, done.set()                 |
```

## Edge cases

- A high-priority task submitted while all workers are busy waits; priority orders the queue, it does not preempt running work.
- `notify()` on every schedule matters: a worker sleeping until `t=60` must recompute its timeout when a task for `t=0.1` arrives.
- Cancelling a running task cannot stop it (Python threads cannot be killed); it only prevents reruns and retries.
- Recurring tasks use fixed delay (next run = end of this run + interval), so a slow run never piles up overlapping runs. Fixed rate (Java `scheduleAtFixedRate`) keeps a cadence but can fall behind.
- A recurring task that exhausts its retries stops recurring and is `FAILED`. Some systems prefer "skip this run, keep the schedule"; say which.
- A task that fails after `shutdown()` starts is not retried and ends `FAILED`; retries already waiting are cancelled with the rest of the future work.
- `shutdown()` called from inside a task would join its own worker and hang. Guard it or document it.

## Production follow-ups

**Is it thread-safe?** One `Condition` guards both heaps and every status change, so a task can never be popped by two workers. `fn` runs outside the lock, so a slow task blocks only its own worker. `Condition.wait(timeout)` releases the lock while sleeping and re-checks state on wake, which handles spurious wake-ups. See [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).

**How do tasks survive a restart?** Store them in PostgreSQL: `jobs(id, run_at, priority, status, attempts, payload, locked_until)`. Workers claim with `SELECT ... WHERE status = 'pending' AND run_at <= now() ORDER BY priority, run_at LIMIT 10 FOR UPDATE SKIP LOCKED` and set a lease. On restart, any `running` row whose lease expired goes back to `pending`. Recurring jobs live as schedule rows that a single leader turns into job rows.

**Idempotency and retries?** Durable schedulers are at-least-once: a worker can finish the side effect and crash before marking the job done, so the job runs again. Make tasks idempotent (idempotency key, upserts, "already sent" checks). Exponential backoff with jitter spreads retries so a recovering dependency is not hit by every retry at once. After `max_retries`, move the job to a dead-letter table for a human.

**How do you add cron or new trigger types?** Extract a `Trigger` strategy with `next_run(after) -> float | None`: `Once`, `FixedDelay`, `FixedRate`, `Cron`. `_run` asks the trigger instead of reading `interval`. Task kinds stay Commands, so neither change touches the worker loop.

**What breaks at 10× or across machines?** One lock and one heap pair becomes contended with dozens of workers; shard into several queues. CPU-bound tasks do not speed up with threads because of the GIL: use processes. Across machines, use a shared queue (PostgreSQL `SKIP LOCKED`, Redis, a broker with Celery), and run exactly one schedule leader (Celery beat, or a PostgreSQL advisory lock) so recurring jobs are not enqueued twice. Millions of timers favor a timing wheel over a heap.

**What would you log and measure?** Queue depth by priority, scheduling lag (`start_time - run_at`), run duration percentiles, retries and failures per task type, dead-letter count, and worker utilization. Log task id, attempt and error on every failure.

**How do you prevent low-priority starvation?** Aging: raise a task's effective priority the longer it waits, or reserve a share of workers for low priority. Without it, a steady stream of high-priority work means priority-9 tasks never run.

**How do you stop a hung task?** Give tasks a cooperative cancel token (an `Event` they check) plus a timeout. For code you cannot trust to cooperate, run it in a subprocess you can kill.

## Interview follow-ups

- **Why a Condition and not `time.sleep` polling?** Polling wastes CPU and adds up to one poll interval of latency. `wait(timeout)` sleeps exactly until the next due task and wakes immediately on `notify`.
- **Why two heaps?** Future tasks are ordered by time, due tasks by priority. One combined key gets one of those orders wrong.
- **Fixed rate or fixed delay?** Fixed delay for jobs that must not overlap (most). Fixed rate when the cadence matters, such as metrics every 10 seconds, and runs are much shorter than the interval.
- **How would you build this on `ThreadPoolExecutor`?** One scheduler thread owns the heaps and submits due tasks to the executor. Simpler to reason about, but priority then only applies until a task enters the executor's FIFO queue.
