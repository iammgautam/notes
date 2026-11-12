---
title: Thread pools and executors
section: lld
order: 13
phase: "Phase 4 · Concurrency"
summary: concurrent.futures as Python's ExecutorService, Futures, errors, timeouts, cancellation, chaining with callbacks and asyncio, and sizing a pool.
---

A pool keeps a fixed set of worker threads (or processes) and feeds them tasks from a queue, so you stop paying thread start-up per task and cap concurrency. Java calls it `ExecutorService`; Python's is `concurrent.futures`, with the same API for threads and processes.

## Threads, processes or asyncio

| Workload | Pick | Why |
|---|---|---|
| Blocking I/O with sync libraries (requests, psycopg, boto3) | `ThreadPoolExecutor` | The GIL is released while a thread waits |
| CPU-bound pure Python | `ProcessPoolExecutor` | Each process has its own GIL |
| Thousands of concurrent network calls with async libraries | `asyncio` | One thread; a task costs far less than a thread |
| Async code that must call a sync function | `asyncio.to_thread` | Keeps the event loop free |

```python
import os
from concurrent.futures import ProcessPoolExecutor


def cpu_heavy(n: int) -> int:
    return sum(i * i for i in range(n))


if __name__ == "__main__":   # required: spawn re-imports this module in workers
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as pool:
        out = list(pool.map(cpu_heavy, [200_000] * 8, chunksize=2))
    assert out == [cpu_heavy(200_000)] * 8
```

Arguments and results cross the process boundary by pickling: no lambdas or nested functions, and large payloads cost real time.

## submit, map, as_completed, wait

```python
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait


def fetch(n: int) -> int:
    time.sleep(0.02 * n)              # stands in for an HTTP call
    if n == 3:
        raise ValueError("bad id 3")
    return n * 10


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=4) as pool:   # exit = shutdown(wait=True)
        # map: results in input order; raises the first error while iterating
        assert list(pool.map(fetch, [5, 1, 2])) == [50, 10, 20]

        # submit: one Future per task, handled one by one
        futures = {pool.submit(fetch, n): n for n in [4, 3, 1, 2]}
        ok, errors = [], {}
        for fut in as_completed(futures, timeout=5):  # in completion order
            try:
                ok.append(fut.result())
            except ValueError as e:                   # re-raised from the worker
                errors[futures[fut]] = str(e)
        assert sorted(ok) == [10, 20, 40] and errors == {3: "bad id 3"}

        # wait: return when the first finishes; the rest keep running
        fs = [pool.submit(fetch, n) for n in (6, 1)]
        done, pending = wait(fs, return_when=FIRST_COMPLETED)
        assert [f.result() for f in done] == [10] and len(pending) == 1
```

- **`map`** for a uniform batch where input order matters and one failure can fail the batch.
- **`submit` + `as_completed`** when you want results as they land and per-task error handling.
- **`wait`** with `FIRST_COMPLETED`, `FIRST_EXCEPTION` or `ALL_COMPLETED` to split done from pending.
- An exception inside a task is stored in its `Future`. It surfaces only when you call `result()` or `exception()`.

## Timeouts, cancellation, shutdown

```python
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def slow(stop: threading.Event) -> str:
    for _ in range(100):              # check the flag between small steps
        if stop.is_set():
            return "stopped"
        time.sleep(0.01)
    return "finished"


if __name__ == "__main__":
    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    running = pool.submit(slow, stop)
    queued = pool.submit(slow, stop)  # waits: the only worker is busy

    try:
        running.result(timeout=0.05)
    except TimeoutError:              # the caller gave up; the task did not
        pass
    assert queued.cancel()            # still queued: cancel works
    assert not running.cancel()       # already running: it cannot
    stop.set()                        # cooperative cancellation
    assert running.result() == "stopped"
    pool.shutdown(wait=True, cancel_futures=True)
```

- `result(timeout=...)` raises `TimeoutError` but the task keeps running. Python cannot kill a thread, so long tasks need a stop flag (`Event`) they check.
- `shutdown(cancel_futures=True)` (3.9+) drops queued tasks; running ones still finish.

## Chaining like CompletableFuture

`add_done_callback(fn)` runs `fn(future)` in the thread that completed the future, or right away in your thread if it is already done. That is enough for `thenApply`:

```python
import threading
from concurrent.futures import Future, ThreadPoolExecutor


def then(fut: Future, fn) -> Future:
    out: Future = Future()

    def chain(done: Future) -> None:
        try:
            out.set_result(fn(done.result()))
        except Exception as e:        # errors flow down the chain
            out.set_exception(e)

    fut.add_done_callback(chain)      # no thread blocks waiting
    return out


def submit_bounded(pool, slots: threading.BoundedSemaphore, fn, *args) -> Future:
    slots.acquire()                   # backpressure: block when too many in flight
    fut = pool.submit(fn, *args)
    fut.add_done_callback(lambda _: slots.release())
    return fut


if __name__ == "__main__":
    with ThreadPoolExecutor() as pool:
        total = then(then(pool.submit(lambda: 100), lambda p: p * 1.18), round)
        assert total.result(timeout=1) == 118
        failed = then(pool.submit(lambda: 1 / 0), lambda x: x + 1)
        assert isinstance(failed.exception(timeout=1), ZeroDivisionError)

        slots = threading.BoundedSemaphore(3)
        fs = [submit_bounded(pool, slots, lambda i=i: i * i) for i in range(50)]
        assert [f.result() for f in fs] == [i * i for i in range(50)]
```

In practice, chaining reads better with `asyncio`, where `await` is `thenApply`/`thenCompose` and `gather` is `allOf`:

```python
import asyncio
import time


def blocking_lookup(n: int) -> int:   # a sync driver you cannot change
    time.sleep(0.05)
    return n


async def fetch(n: int) -> int:
    await asyncio.sleep(0.05)         # non-blocking I/O
    if n < 0:
        raise ValueError(n)
    return n * 2


async def main() -> None:
    start = time.perf_counter()
    assert await asyncio.gather(*(fetch(i) for i in range(100))) == [i * 2 for i in range(100)]
    assert time.perf_counter() - start < 1           # 100 calls overlapped

    task = asyncio.create_task(fetch(1))             # starts now, awaited later
    other = await asyncio.to_thread(blocking_lookup, 7)
    assert await task + other == 9                   # thenCombine

    res = await asyncio.gather(fetch(1), fetch(-1), return_exceptions=True)
    assert res[0] == 2 and isinstance(res[1], ValueError)

    try:
        async with asyncio.timeout(0.01):            # 3.11+, cancels the await
            await fetch(1)
    except TimeoutError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
```

## Sizing a pool

- **CPU-bound, processes:** `os.cpu_count()`. In a container it reports the host's CPUs, not your CPU quota; size from the quota.
- **I/O-bound, threads:** `1 + wait_time / compute_time` per core of real parallelism. Under the GIL treat it as one core: 50 ms waiting per 5 ms of Python work gives about 11 threads.
- **Default:** `ThreadPoolExecutor()` uses `min(32, os.cpu_count() + 4)`.
- **Cap by the downstream:** no more workers than DB connections or the API's rate limit allows. Extra threads only queue at the bottleneck.
- `ThreadPoolExecutor`'s work queue is unbounded, so `submit()` never blocks. Use a semaphore like `submit_bounded` for backpressure.

## Pitfalls

- **Fire-and-forget `submit`**: the exception sits in a Future no one reads, and nothing is logged. Call `result()` or attach a callback that logs.
- **Pool starvation**: tasks that submit to the same pool and wait on the result. Once every worker is waiting, nothing runs.
- **A pool per request**: create one at startup and reuse it.
- **Blocking calls inside `async def`** (`time.sleep`, `requests`) freeze the whole event loop. Use `to_thread`.

## Interview questions

- **`submit` vs `map`?** `map` returns results in input order and stops at the first error; `submit` gives a Future per task for per-task handling and `as_completed`.
- **How do you cancel a running task?** You cannot. `cancel()` only works on queued tasks; running ones must check an `Event`.
- **Where do exceptions go?** Into the Future; `result()` re-raises them in the caller.
- **Threads or processes for image resizing in pure Python?** Processes: CPU-bound work does not speed up across threads under the GIL.
