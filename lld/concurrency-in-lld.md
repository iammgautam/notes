---
title: Concurrency in real designs
section: lld
order: 18
phase: "Phase 4 · Concurrency"
summary: Making parking lots, rate limiters, caches and booking systems thread-safe, picking lock granularity, and the top 10 concurrency interview questions.
---

Bring concurrency up whenever two actors can touch the same mutable thing: a spot, a seat, a balance, a token bucket, a cache entry. Naming the race before you're asked is what separates a mid-level answer from a senior one.

## The one race to look for: check-then-act

```python
# Two threads both see the spot free, both take it.
if spot.is_free():
    spot.assign(vehicle)
```

The check and the act must be one atomic step. Same shape: read-modify-write (`count += 1`), get-or-create (`if key not in d: d[key] = ...`), lazy init (`if x is None: x = ...`).

> **Gotcha:** The GIL makes single bytecodes atomic, not your logic. `count += 1` is a load, an add and a store; another thread can run between them. On free-threaded Python (3.13t) threads run truly in parallel, so races you got away with show up far more often.

## Parking lot entry and exit at scale

Several gates park cars at once. One lock per floor lets floors work in parallel, and each gate starts on a different floor so they don't all fight over floor 0.

```python
import threading


class Floor:
    def __init__(self, number: int, spots: int):
        self.number = number
        self._free = list(range(spots))     # free spot numbers
        self._lock = threading.Lock()       # guards this floor only

    def try_park(self) -> int | None:
        with self._lock:                    # check + take in one step
            return self._free.pop() if self._free else None

    def leave(self, spot: int) -> None:
        with self._lock:
            self._free.append(spot)


class ParkingLot:
    def __init__(self, floors: int, spots_per_floor: int):
        self.floors = [Floor(i, spots_per_floor) for i in range(floors)]

    def park(self, gate: int) -> tuple[int, int] | None:
        n = len(self.floors)
        for k in range(n):                  # each gate starts on its own floor
            floor = self.floors[(gate + k) % n]
            spot = floor.try_park()         # one floor lock at a time
            if spot is not None:
                return floor.number, spot
        return None                         # full (when we looked)

    def leave(self, ticket: tuple[int, int]) -> None:
        floor, spot = ticket
        self.floors[floor].leave(spot)


if __name__ == "__main__":
    lot = ParkingLot(floors=3, spots_per_floor=10)
    tickets: list[tuple[int, int]] = []
    lock = threading.Lock()

    def car(i: int) -> None:
        ticket = lot.park(gate=i % 4)
        if ticket:
            with lock:
                tickets.append(ticket)

    cars = [threading.Thread(target=car, args=(i,)) for i in range(200)]
    for t in cars:
        t.start()
    for t in cars:
        t.join()
    assert len(tickets) == len(set(tickets)) == 30   # full, no spot twice
    for ticket in tickets:
        lot.leave(ticket)
    assert lot.park(gate=0) is not None
    print("ok")
```

No thread ever holds two floor locks, so there is no lock order to get wrong.

| Option | Throughput | Works across servers | Use when |
|---|---|---|---|
| One global lock | One car at a time | No | Interview default, one process |
| Lock per floor | Floors in parallel | No | Many gates, one process |
| Atomic DB update | Row locks, gates skip each other | Yes | Production, several gate servers |

In production the database is the lock. `SKIP LOCKED` makes a gate pass over a spot another gate is claiming instead of waiting for it:

```sql
UPDATE spots SET vehicle_id = %s, taken_at = now()
WHERE id = (
    SELECT id FROM spots
    WHERE lot_id = %s AND vehicle_id IS NULL
    ORDER BY floor, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, floor;
```

A partial unique index on `vehicle_id` stops one car holding two spots, and `UPDATE ... SET vehicle_id = NULL WHERE id = %s AND vehicle_id = %s` makes exit idempotent: a retry updates zero rows. Full design: [parking lot]({{ '/lld/parking-lot/' | relative_url }}).

## Rate limiter across threads

Refill, check and take are one critical section per bucket. The map of buckets has its own lock, held only for get-or-create, so clients never wait on each other.

```python
import threading
import time


class TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate, self.capacity = rate, capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:                    # refill + check + take: one step
            now = time.monotonic()
            self._tokens = min(self.capacity,
                               self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


class RateLimiter:
    def __init__(self, rate: float, capacity: int):
        self.rate, self.capacity = rate, capacity
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()       # guards the dict, held briefly

    def allow(self, client: str) -> bool:
        with self._lock:                    # get-or-create is check-then-act
            bucket = self._buckets.get(client)
            if bucket is None:
                bucket = self._buckets[client] = TokenBucket(self.rate,
                                                             self.capacity)
        return bucket.allow()               # per-client lock


if __name__ == "__main__":
    limiter = RateLimiter(rate=0.001, capacity=100)
    allowed = {"a": 0, "b": 0}
    lock = threading.Lock()

    def hammer(client: str) -> None:
        for _ in range(50):
            if limiter.allow(client):
                with lock:
                    allowed[client] += 1

    ts = [threading.Thread(target=hammer, args=(c,)) for c in "ab" * 20]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert allowed == {"a": 100, "b": 100}  # 1,000 tries each, 100 tokens
    print("ok")
```

Across processes a `threading.Lock` protects nothing. Move the bucket to Redis and make refill-and-take atomic there: a Lua script (runs as one command), or `INCR` plus `EXPIRE` for a fixed window. At low rates a conditional `UPDATE ... WHERE tokens >= 1` in the database also works. More in [rate limiter]({{ '/lld/rate-limiter/' | relative_url }}).

## Thread-safe cache

- A single `dict` get or set is atomic in CPython; **get-or-load is not**. Lock it, or accept duplicate loads.
- **LRU:** `get` reorders, so even reads write. One plain `Lock` around `get` and `put`. See [LRU cache]({{ '/lld/lru-cache/' | relative_url }}).
- **Read-mostly, no reordering:** a read-write lock lets readers overlap; with the GIL and fast dict reads a plain lock is often as fast, so measure. See [read-write lock cache]({{ '/lld/read-write-lock-cache/' | relative_url }}).
- **Never load under the cache lock.** Keep a per-key in-flight `Future` ("single flight"): the first miss loads, the other 99 wait on it, and the DB sees one query.

## Booking systems: seat holds

- **In memory:** one lock per show. Check and hold *all* requested seats in one critical section (all-or-nothing). A hold has an expiry; payment runs **outside** the lock; confirm re-checks the hold is still yours.
- **Optimistic (version column):** update only if the version you read is unchanged. Zero rows means you lost: re-read and retry, or tell the user. Good when conflicts are rare.

```sql
UPDATE seats
SET status = 'held', held_by = %s,
    hold_expires = now() + interval '5 minutes', version = version + 1
WHERE show_id = %s AND seat_no = %s AND version = %s
  AND (status = 'free' OR (status = 'held' AND hold_expires < now()));
```

- **Pessimistic:** `SELECT ... FOR UPDATE` (Django `select_for_update()`). Better for a hot show where most optimistic attempts would conflict.
- **Last line of defense:** a unique index on `(show_id, seat_no)` over active bookings (partial, `WHERE status <> 'cancelled'`); the database refuses the second row.
- Full designs: [movie ticket booking]({{ '/lld/movie-ticket-booking/' | relative_url }}), [hotel booking]({{ '/lld/hotel-booking/' | relative_url }}).

## Choosing lock granularity

| Granularity | Pros | Cons |
|---|---|---|
| One global lock | Simple, can't deadlock | Serializes everything |
| Per entity (spot, seat, account) | Most parallelism | Multi-entity ops need a lock order |
| Striping (N locks, `hash(id) % N`) | Bounded memory | Unrelated keys sometimes collide |
| No lock: single writer thread + `queue.Queue` | No races by construction | One thread's throughput |

- One lock per **invariant**: whatever must stay consistent together sits under the same lock.
- Start coarse and name the upgrade; a global lock is a fine interview answer if you say what you'd split next.
- Never hold a lock across I/O, a callback, or a call into another object that takes its own lock.
- Immutable value objects (`@dataclass(frozen=True)`) need no lock.

## Top 10 concurrency interview questions

1. **Thread-safe singleton with double-checked locking.** Check without the lock, lock, check again, create; the second check stops two threads that both passed the first. Java also needs `volatile` against half-built objects. In Python a module-level instance is already a singleton (imports are locked). [Thread-safe singleton]({{ '/lld/thread-safe-singleton/' | relative_url }}).
2. **Producer-consumer with a blocking queue.** `queue.Queue(maxsize=N)` (Java `BlockingQueue`): `put` blocks when full (backpressure), `get` when empty; one sentinel per consumer to stop. From scratch: one lock, two `Condition`s, `while` loops around `wait`. [Producer-consumer]({{ '/lld/producer-consumer/' | relative_url }}).
3. **Rate limiter across threads.** Token bucket per client, refill-check-take under its lock, a short separate lock for get-or-create. Across servers: Redis plus a Lua script. [Rate limiter]({{ '/lld/rate-limiter/' | relative_url }}).
4. **Lock vs RLock.** `Lock` isn't re-entrant: acquiring it twice in one thread deadlocks. `RLock` counts its owner's re-entries. Java's `synchronized` is re-entrant; `ReentrantLock` adds `tryLock`, fairness and multiple conditions. Prefer `Lock`; needing `RLock` usually means one locked method calls another. [Python concurrency primitives]({{ '/lld/python-concurrency-primitives/' | relative_url }}).
5. **Connection pool borrow/return.** `BoundedSemaphore(N)` caps open connections; borrow with a timeout, validate on borrow, return in `finally` via a context manager. [Connection pool]({{ '/lld/connection-pool/' | relative_url }}).
6. **Cache with read-write locks.** Many readers or one writer. Worth it when reads dominate and are slow; prefer waiting writers or they starve. Not for LRU, whose reads mutate. [Read-write lock cache]({{ '/lld/read-write-lock-cache/' | relative_url }}).
7. **Dining philosophers.** Everyone holding their left fork is circular wait. Take the lower-numbered fork first, or use a waiter `Semaphore(n - 1)`. Test with `join(timeout)` and assert no thread is alive. [Deadlock, livelock and starvation]({{ '/lld/deadlock-livelock-starvation/' | relative_url }}).
8. **Task scheduler with thread pools.** A min-heap of `(run_at, seq, task)` under a `Condition`. A dispatcher sleeps until the earliest `run_at`, is woken by `notify` when an earlier task arrives, and hands due tasks to a `ThreadPoolExecutor` (Java `ScheduledExecutorService`). [Task scheduler]({{ '/lld/task-scheduler/' | relative_url }}), [thread pools and executors]({{ '/lld/thread-pools-and-executors/' | relative_url }}).
9. **CountDownLatch from scratch.** A count and a `Condition`: `count_down` decrements and calls `notify_all` at zero; `wait` is `wait_for(lambda: count == 0, timeout)`. One-shot; use a barrier for rounds. [Latches and barriers]({{ '/lld/latch-and-barrier/' | relative_url }}).
10. **Thread-safe parking lot at scale.** Check-and-take under a per-floor lock, gates start on different floors, no thread holds two locks. Across servers the DB is the lock: `FOR UPDATE SKIP LOCKED`. [Parking lot]({{ '/lld/parking-lot/' | relative_url }}).

> **Interview tip:** Point at the exact method and say "check-then-act must be atomic here", then name the lock and what it guards.
