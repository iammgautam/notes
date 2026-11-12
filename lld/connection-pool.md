---
title: Design a connection pool
section: lld
order: 15
phase: "Phase 4 · Concurrency"
summary: A bounded, thread-safe pool with borrow timeouts, validation on borrow, lifetime and idle eviction, leak detection and a stress test.
---

A connection pool keeps a few open database connections and lends them to threads. Opening a PostgreSQL connection costs a TCP and TLS handshake, auth and a new server process (milliseconds, and a few MB on the server), so you pay once and reuse it.

## The problem it solves

- **Latency:** borrowing an idle connection takes microseconds; opening one takes milliseconds.
- **A hard cap:** the database has a connection limit (`max_connections`, 100 by default in PostgreSQL). The pool makes sure one process never holds more than N.
- **Backpressure:** when all N are busy, callers wait (with a timeout) instead of piling load onto the database.

## Design in five lines

| Concern | Mechanism |
|---|---|
| Never more than N open | `BoundedSemaphore(N)`: a slot per open connection |
| Borrow timeout | `semaphore.acquire(timeout=...)`, raise `PoolTimeout` |
| Dead or stale connections | Validate on borrow: `ping()`, max lifetime, max idle |
| Always return | Context manager with `try/finally` |
| Leaks | Map of borrowed connection to borrow time, `leaks(older_than)` |

Idle connections sit in a `deque` used as a stack (LIFO): borrowing the most recently returned one keeps a few hot and lets the rest age out, so idle eviction can close them. `deque.append` and `pop` are thread-safe on their own.

## Code

```python
import itertools
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager


class PoolTimeout(Exception):
    pass


class Connection:
    """Stand-in for a DB connection (psycopg, etc.)."""
    _ids = itertools.count(1)

    def __init__(self):
        self.id = next(self._ids)
        self.created_at = time.monotonic()
        self.closed = False

    def ping(self) -> bool:              # a real pool runs SELECT 1
        return not self.closed

    def close(self) -> None:
        self.closed = True


class ConnectionPool:
    def __init__(self, factory: Callable[[], Connection], max_size: int = 5,
                 max_lifetime: float = 1800.0, max_idle: float = 300.0):
        self._factory = factory
        self._max_lifetime, self._max_idle = max_lifetime, max_idle
        self._slots = threading.BoundedSemaphore(max_size)  # N open, max
        self._idle: deque[tuple[Connection, float]] = deque()  # (conn, t)
        self._lock = threading.Lock()            # guards _in_use only
        self._in_use: dict[int, float] = {}      # conn id -> borrowed at

    def acquire(self, timeout: float = 5.0) -> Connection:
        if not self._slots.acquire(timeout=timeout):
            raise PoolTimeout(f"no connection free within {timeout}s")
        try:
            conn = self._take_healthy()          # I/O happens outside locks
        except BaseException:
            self._slots.release()                # connect failed: free slot
            raise
        with self._lock:
            self._in_use[conn.id] = time.monotonic()
        return conn

    def release(self, conn: Connection) -> None:
        with self._lock:
            if self._in_use.pop(conn.id, None) is None:
                raise ValueError("connection is not borrowed from this pool")
        if not conn.closed:
            self._idle.append((conn, time.monotonic()))  # before the slot
        self._slots.release()

    @contextmanager
    def connection(self, timeout: float = 5.0):
        conn = self.acquire(timeout)
        try:
            yield conn
        finally:
            self.release(conn)                   # on return, raise or break

    def leaks(self, older_than: float) -> list[int]:
        cutoff = time.monotonic() - older_than
        with self._lock:
            return [cid for cid, t in self._in_use.items() if t < cutoff]

    def reap(self) -> int:
        """Close expired idle conns, oldest first. Call from a timer."""
        closed = 0
        # Hold a slot while inspecting, so borrowers can't open an extra one.
        while self._idle and self._slots.acquire(blocking=False):
            try:
                conn, returned_at = self._idle.popleft()
            except IndexError:
                self._slots.release()
                break
            expired = self._expired(conn, returned_at)
            if expired:
                conn.close()
                closed += 1
            else:
                self._idle.appendleft((conn, returned_at))  # rest are newer
            self._slots.release()
            if not expired:
                break
        return closed

    def _take_healthy(self) -> Connection:
        while True:
            try:
                conn, returned_at = self._idle.pop()  # LIFO: warmest first
            except IndexError:
                return self._factory()
            if self._expired(conn, returned_at) or not conn.ping():
                conn.close()
                continue
            return conn

    def _expired(self, conn: Connection, returned_at: float) -> bool:
        now = time.monotonic()
        return (now - conn.created_at > self._max_lifetime
                or now - returned_at > self._max_idle)


if __name__ == "__main__":
    class Gauge:
        def __init__(self):
            self.value = self.peak = 0
            self._lock = threading.Lock()

        def add(self, delta: int) -> None:
            with self._lock:
                self.value += delta
                self.peak = max(self.peak, self.value)

    in_use, open_conns = Gauge(), Gauge()

    class CountedConnection(Connection):
        def __init__(self):
            super().__init__()
            open_conns.add(1)

        def close(self):
            if not self.closed:
                open_conns.add(-1)
            super().close()

    pool = ConnectionPool(CountedConnection, max_size=5)

    def worker():
        for _ in range(50):
            with pool.connection() as conn:
                in_use.add(1)
                if conn.id % 7 == 0:
                    conn.close()                 # server dropped it
                time.sleep(0.0005)
                in_use.add(-1)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert in_use.peak <= 5                      # never more than N borrowed
    assert open_conns.peak <= 5                  # never more than N open
    assert pool.leaks(older_than=0) == []

    small = ConnectionPool(Connection, max_size=1)
    held = small.acquire()
    try:
        small.acquire(timeout=0.05)
        raise AssertionError("expected PoolTimeout")
    except PoolTimeout:
        pass
    assert small.leaks(older_than=0) == [held.id]
    small.release(held)
    held.close()                                 # dies while idle
    fresh = small.acquire()
    assert fresh is not held and not fresh.closed  # validated on borrow
    small.release(fresh)

    quick = ConnectionPool(Connection, max_size=2, max_idle=0.01)
    a, b = quick.acquire(), quick.acquire()
    quick.release(a)
    quick.release(b)
    time.sleep(0.02)
    assert quick.reap() == 2 and a.closed and b.closed
    print("ok")
```

Why it never opens more than N: a thread creates a connection only while holding a slot and only after finding the idle stack empty. `release` pushes the connection back before freeing its slot, and `reap` holds a slot while a connection is out of the stack, so a connection is always either idle or counted against a slot.

## How real pools map to this

| This note | SQLAlchemy `QueuePool` | `psycopg_pool` | HikariCP (Java) |
|---|---|---|---|
| `max_size` | `pool_size` + `max_overflow` | `max_size` | `maximumPoolSize` |
| borrow `timeout` | `pool_timeout` | `timeout` | `connectionTimeout` |
| `ping()` on borrow | `pool_pre_ping=True` | `check=` callback | validation query |
| `max_lifetime` | `pool_recycle` | `max_lifetime` | `maxLifetime` |
| `max_idle` + `reap` | (none) | `max_idle` | `idleTimeout` |
| `leaks()` | (none) | (none) | `leakDetectionThreshold` |

- SQLAlchemy also **resets on return** (rolls back any open transaction) so the next borrower doesn't inherit half-done work. A `conn.rollback()` in `release` does the same.
- Django: `CONN_MAX_AGE` keeps one connection per thread; since Django 5.1, `"OPTIONS": {"pool": True}` on the PostgreSQL backend uses `psycopg_pool`.
- **PgBouncer** is a pool in its own process, shared by every app process. In `transaction` mode a client gets a server connection for one transaction only, so 1,000 clients can share 50 server connections; session state (`SET`, session advisory locks, `LISTEN`) doesn't survive between transactions. The two stack: the app pool saves connect latency, PgBouncer caps what PostgreSQL sees.

## When to use / when not

- Use for anything expensive to open and safe to reuse: DB connections, HTTP keep-alive sessions, gRPC channels.
- In production use the driver's or ORM's pool; build one in an interview to show the semaphore, the timeout and the `finally`.
- Skip it for cheap objects; pooling them is slower than allocating.

## Pitfalls

- **Returning a broken connection.** After a network error it may be dead or mid-transaction: close it (the slot is freed) or roll it back.
- **Validating under a lock.** `ping()` is a network round trip; here it runs holding only a slot, never `_lock`.
- **Double release** pushes one connection twice and over-releases the semaphore. The `_in_use` check raises; `BoundedSemaphore` is a second guard.
- **Borrowing twice in one thread.** Five threads each holding one of five connections and asking for a second wait forever. Pass the connection down.
- **Holding a connection across slow I/O** (an HTTP or LLM call) drains the pool. Borrow late, return early.

## Production follow-ups

**Thread safety?** Only the semaphore blocks; `_lock` guards a dict for microseconds; network I/O happens outside both. Async code needs an async pool (`psycopg_pool.AsyncConnectionPool`), not a thread one.

**How big should the pool be?** Smaller than you think: the database runs about as many queries in parallel as it has cores, and the rest queue there. Start near 2 × database cores in total, and keep `processes × pool size` under `max_connections`. 20 Gunicorn workers × 10 is already 200: time for PgBouncer.

**What happens when the database restarts?** Every idle connection is dead. Validation on borrow (`pre_ping`) turns that into a failed ping and a fresh connect instead of a failed request. `max_lifetime` rotates connections so a failover or DNS change is picked up within minutes.

**What do you measure?** Borrow wait (p99), in-use vs max, `PoolTimeout` count, connections opened per minute (churn means lifetime is too short or validation fails), and leaks. For leaks, log where the connection was borrowed (`traceback.format_stack()`, debug mode only: it's expensive).

## Interview questions

- **Semaphore or `queue.Queue`?** Both work. A `Queue(maxsize=N)` pre-filled with N connections is the shortest version; the semaphore lets you open connections lazily and replace dead ones without tracking a queue size.
- **How do you avoid leaks?** Lend only through a context manager, track borrow times, alert past a threshold.
- **Why LIFO?** Hot connections stay hot and idle ones age out. FIFO cycles through all of them, so none ever looks idle.
- **What if the factory raises?** Release the slot before re-raising, or every failed connect permanently shrinks the pool.

Related: [Python concurrency primitives]({{ '/lld/python-concurrency-primitives/' | relative_url }}), [producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).
