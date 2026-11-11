---
title: Concurrency primitives
section: os-concurrency
order: 3
summary: Mutexes, RW locks, semaphores, condition variables and atomics, what each guarantees, and their Python threading, asyncio and distributed equivalents.
---

Whenever two threads, coroutines, processes or servers touch shared state, you need a primitive that controls who goes when. The primitives are the same few ideas everywhere, from the kernel to `threading` to Postgres to Redis. Knowing what each one guarantees, and what it doesn't, lets you pick the smallest tool that is actually correct.

## How it works

### The underlying problem

`counter += 1` is read, add, write. Two threads can both read 41 and both write 42. Any **read-modify-write** on shared state needs either mutual exclusion or an atomic operation. In CPython the GIL makes single bytecodes atomic, but `+=` on an attribute is several bytecodes, so it is not safe, and free-threaded builds remove even that accidental protection.

### Mutex (lock)

Only one holder at a time; others block until it's released. Protects a **critical section**.

- Keep critical sections short; never do I/O while holding a lock if you can avoid it.
- A **reentrant** lock (`RLock`) can be re-acquired by the thread that holds it; a plain lock deadlocks if you do that.
- Linux implements them with futexes: uncontended acquire is a single atomic instruction in user space; only contention enters the kernel.

### Read-write lock

Many readers **or** one writer. Useful when reads vastly outnumber writes and reads take a while. Risks writer starvation if readers keep arriving; implementations usually prefer writers once one is waiting. Python's stdlib doesn't ship one; Postgres row locks (`FOR SHARE` vs `FOR UPDATE`) are the same idea.

### Semaphore

A counter of permits. `acquire` takes a permit (blocks at zero), `release` returns one. A semaphore of N limits concurrency to N: "at most 5 concurrent calls to the LLM provider", "at most 10 DB connections". `BoundedSemaphore` raises if released more times than acquired, catching bugs.

### Condition variable

Lets a thread **wait until a condition becomes true**, atomically releasing the associated lock while it sleeps. The classic producer/consumer building block. Always wait in a loop, because wakeups can be spurious and another thread may have consumed the item first.

### Event and barrier

- **Event**: a one-shot flag. Threads `wait()` until someone calls `set()`. Good for "shutdown requested" or "config loaded".
- **Barrier**: N parties wait until all N arrive, then all proceed.

### Atomics and compare-and-swap

CPUs provide atomic instructions: fetch-and-add, and **compare-and-swap** (CAS): "set X to new only if it still equals old; tell me if it worked". Lock-free structures retry CAS in a loop. You rarely write these in Python, but the pattern is everywhere at the application layer: `UPDATE ... SET n = n + 1`, `UPDATE ... WHERE version = 7`, Redis `INCR`, `SET key val NX`.

### Memory visibility

In languages like Java, C++ and Go, locks also act as **memory barriers**: writes made before releasing a lock are visible to the next thread that acquires it. Without them, one thread may never see another's write. The GIL hides this in CPython today; free-threaded Python and other languages make it real. The rule: shared mutable state is only read and written under the same lock.

## In practice

### Python threading

```python
import threading

class Counter:
    def __init__(self):
        self._lock = threading.Lock()
        self._n = 0

    def incr(self):
        with self._lock:            # always use the context manager
            self._n += 1
            return self._n
```

Bounded concurrency to an external API from a thread pool:

```python
from concurrent.futures import ThreadPoolExecutor

llm_slots = threading.BoundedSemaphore(5)

def embed(chunk):
    with llm_slots:                 # at most 5 in flight
        return client.embeddings.create(input=chunk, model="text-embedding-3-small")

with ThreadPoolExecutor(max_workers=20) as pool:
    vectors = list(pool.map(embed, chunks))
```

Producer/consumer: prefer `queue.Queue`, which is a lock plus two condition variables already done right.

```python
import queue

q = queue.Queue(maxsize=100)        # bounded = backpressure

def producer(rows):
    for row in rows:
        q.put(row)                  # blocks when full
    q.put(None)                     # sentinel

def consumer():
    while (row := q.get()) is not None:
        process(row)
```

Raw condition variable, for when you need it:

```python
cond = threading.Condition()
ready = []

def wait_for_item(timeout=5):
    with cond:
        if not cond.wait_for(lambda: ready, timeout=timeout):  # loops internally
            raise TimeoutError
        return ready.pop(0)

def add_item(x):
    with cond:
        ready.append(x)
        cond.notify()
```

### asyncio equivalents

`asyncio.Lock`, `Semaphore`, `Event`, `Condition` and `Queue` mirror the threading API but coordinate coroutines on one event loop. They are **not** thread-safe and do not work across processes.

```python
sem = asyncio.Semaphore(10)

async def fetch(session, url):
    async with sem:
        async with session.get(url) as r:
            return await r.text()
```

Even in single-threaded asyncio you need locks if a critical section contains an `await`, because another coroutine can run at that point.

### Across processes and servers

`threading.Lock` does nothing between gunicorn workers or Celery children, let alone between pods. Options, cheapest first:

| Need | Primitive |
|---|---|
| Atomic counter | `UPDATE t SET n = n + 1`, Redis `INCR` |
| Mutual exclusion on a row | `SELECT ... FOR UPDATE` in a transaction |
| Mutual exclusion on an arbitrary key | Postgres advisory lock |
| Compare-and-swap | `UPDATE ... WHERE version = %s`, check rowcount |
| "Only one" constraint | unique index |
| Concurrency limit across workers | Redis-based semaphore, or a Celery queue with fixed concurrency |

```python
from django.db import connection, transaction

def run_exclusively(tenant_id):
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [42, tenant_id])
        if not cur.fetchone()[0]:
            return  # another worker is already doing this tenant
        rebuild_rollups(tenant_id)
    # lock released at commit/rollback
```

Redis lock with expiry and owner token:

```python
import uuid

token = str(uuid.uuid4())
if r.set(f"lock:report:{tenant_id}", token, nx=True, ex=60):
    try:
        build_report(tenant_id)
    finally:
        # release only if we still own it (Lua for atomic check-and-delete)
        r.eval("if redis.call('get', KEYS[1]) == ARGV[1] then "
               "return redis.call('del', KEYS[1]) end return 0",
               1, f"lock:report:{tenant_id}", token)
```

A Redis lock with a TTL is a **lease**: if your work outlives the TTL, someone else gets the lock. For correctness-critical sections, back it with a database constraint or fencing token.

## Pitfalls

- **Process-local locks in multi-process servers.** Works in `runserver`, does nothing in production.
- **Forgetting to release on exceptions.** Always `with lock:`.
- **Holding a lock across I/O.** Network calls under a lock serialize the whole service.
- **Non-reentrant lock re-acquired** in a nested call: instant self-deadlock.
- **`if` instead of `while` around `wait()`**: acting on a condition that is no longer true.
- **Unbounded queues**: producers outrun consumers until memory runs out. Bound them.
- **Mixing asyncio and threading primitives**: an `asyncio.Lock` used from a thread, or a `threading.Lock` blocking the event loop.
- **Locks without timeouts** in distributed setups. A crashed holder never releases.

## Rules of thumb

- Prefer no shared mutable state, then message passing (queues), then locks.
- Use the database's atomic operations before inventing a lock.
- Semaphores for "at most N", queues for producer/consumer, events for one-shot signals.
- Every distributed lock has a timeout and an owner token.
- Acquire multiple locks in a fixed global order (see [Deadlocks, livelocks and starvation]({{ '/os-concurrency/deadlocks/' | relative_url }})).

## Interview angle

- **"Mutex vs semaphore?"** A mutex allows one holder and has an owner; a semaphore is a counter that allows up to N concurrent holders and can be released by anyone.
- **"What is a condition variable for?"** Waiting efficiently until shared state satisfies a predicate, releasing the lock while waiting and re-checking in a loop after wakeup.
- **"How do you implement a lock across multiple servers?"** Postgres row or advisory locks within a transaction, or a Redis `SET NX EX` lease with an owner token; for correctness rely on DB constraints or fencing tokens rather than the lease alone.
