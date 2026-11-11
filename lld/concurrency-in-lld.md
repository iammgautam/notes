---
title: Concurrency in LLD
section: lld
order: 7
summary: Where race conditions hide in classic LLD problems and how to make designs thread-safe with locks, lock granularity, atomic ops and DB-level guards.
---

## When to bring it up

Any time two actors can touch the **same mutable resource**: a seat, a parking spot, a wallet balance, an inventory count, a cache entry, a rate-limit bucket. Mention it even if not asked; it separates mid from senior answers.

## The core race: check-then-act

```python
# Two threads both see the spot free, both take it
if spot.is_free():
    spot.assign(vehicle)
```

The check and the act must be **atomic** together. Same shape: read-modify-write (`count += 1`), lazy init (`if x is None: x = ...`).

> **Gotcha:** The GIL does not save you. It makes single bytecodes atomic, not your multi-step logic. `count += 1` is several bytecodes.

## Tools in Python

| Tool | Use for |
|------|---------|
| `threading.Lock` | Mutual exclusion around a critical section |
| `threading.RLock` | Same thread may re-acquire (nested calls) |
| `threading.Condition` | Wait until a predicate holds (producer/consumer) |
| `threading.Semaphore` | Limit concurrent access to N |
| `queue.Queue` | Thread-safe handoff; avoids explicit locks |
| `concurrent.futures` | Thread/process pools |

```python
import threading


class Spot:
    def __init__(self, id_):
        self.id, self.vehicle = id_, None
        self._lock = threading.Lock()

    def try_assign(self, vehicle) -> bool:
        with self._lock:                # check + act under one lock
            if self.vehicle is not None:
                return False
            self.vehicle = vehicle
            return True
```

## Lock granularity

| Granularity | Pros | Cons |
|-------------|------|------|
| One global lock | Simple, no deadlocks | Serializes everything |
| Per-entity lock (per spot, per seat, per account) | High concurrency | Deadlock risk when taking several |
| Lock striping (N locks, hash id → lock) | Bounded memory | Some false contention |

> **Rule of thumb:** Start with a per-resource lock. Say a global lock is fine for an in-memory interview solution, and name the upgrade.

## Deadlocks

Happen when threads acquire multiple locks in different orders (transfer A→B while another does B→A).

- **Fix:** acquire in a global order (sort by id).
- Or use `lock.acquire(timeout=...)` and back off.

```python
def transfer(a, b, amount):
    first, second = sorted((a, b), key=lambda acc: acc.id)
    with first.lock, second.lock:
        a.balance -= amount
        b.balance += amount
```

## Where races appear in classic problems

| Problem | Race | Fix |
|---------|------|-----|
| Parking lot | Two cars assigned the same spot | Lock per spot or per floor; atomic `try_assign` |
| Movie booking | Double booking a seat | Lock seats in sorted order; temporary hold with expiry; DB unique constraint |
| Splitwise | Concurrent expense updates corrupt balances | Lock per group, or append-only ledger |
| LRU cache | Linked list corrupted by concurrent `get`/`put` | One lock around both (even `get` mutates order) |
| Rate limiter | Two requests both take the last token | Lock per bucket; Redis `INCR` / Lua script |
| Elevator | Request list mutated while scheduler iterates | `queue.Queue` or lock around request set |
| Inventory / vending | Oversell last item | Atomic decrement with check |
| Singleton | Two instances created | Double-checked locking |

## Beyond one process: DB-level guards

Interview LLD is in-memory, but say how it maps to production (Django + PostgreSQL):

- **Pessimistic:** `SELECT ... FOR UPDATE` (`select_for_update()`) inside a transaction.
- **Optimistic:** a `version` column; `UPDATE ... WHERE id=? AND version=?`, retry on 0 rows.
- **Constraints:** `UNIQUE(show_id, seat_id)` on bookings makes double booking impossible.
- **Atomic update:** `F("stock") - 1` with `filter(stock__gt=0)`.
- **Distributed lock / TTL hold:** Redis `SET key val NX PX 300000` for seat holds.

```python
from django.db import transaction
from django.db.models import F

with transaction.atomic():
    updated = (Item.objects
               .filter(id=item_id, stock__gt=0)
               .update(stock=F("stock") - 1))
    if not updated:
        raise OutOfStock()
```

## Design patterns that reduce locking

- **Immutable value objects** (`@dataclass(frozen=True)`): safe to share.
- **Single-writer / actor:** one thread owns the state, others send commands through a `queue.Queue`.
- **Confinement:** keep state thread-local.
- **Event log:** append-only records, derive balances; appends are easy to make safe.

## Pitfalls

- Locking only the write but not the read that decides it.
- Holding a lock while doing I/O (payment call) → long contention. Hold the seat, release the lock, then pay.
- Calling external callbacks (observers) while holding a lock → deadlocks.
- Forgetting that `dict` ops are atomic individually but sequences of them are not.

> **Interview tip:** Use the phrase "check-then-act must be atomic" and point to the exact method where you add the lock.
