---
title: Deadlocks, livelocks and starvation
section: os-concurrency
order: 4
summary: The four Coffman conditions, how lock ordering and timeouts prevent deadlocks, and how to diagnose and fix PostgreSQL deadlocks in a Django app.
---

A deadlock is a cycle of waiters, each holding something the next one needs. In application code it shows up as a hung worker; in PostgreSQL it shows up as `deadlock detected` errors in Sentry, usually under load and usually from two code paths that update the same rows in different orders. Livelock and starvation are the cousins: everyone is busy but nothing finishes, or one party never gets a turn.

## How it works

### The Coffman conditions

A deadlock requires all four at once:

1. **Mutual exclusion**: a resource can be held by one party only.
2. **Hold and wait**: a party holds one resource while waiting for another.
3. **No preemption**: resources can't be taken away; only the holder releases.
4. **Circular wait**: A waits for B, B waits for A (or a longer cycle).

Break any one and deadlock is impossible. In practice you break **circular wait** (consistent lock ordering) or **hold and wait** (acquire everything at once, or hold less), and add **timeouts** as a form of preemption.

```text
T1: lock(account 1) ----> wants account 2 (held by T2)  ... waits
T2: lock(account 2) ----> wants account 1 (held by T1)  ... waits
            cycle: T1 -> T2 -> T1
```

### Prevention vs detection

- **Prevention**: lock ordering, coarse locks, acquiring all locks up front, shorter transactions.
- **Avoidance**: timeouts and `try_lock`: give up and retry instead of waiting forever.
- **Detection and recovery**: build a wait-for graph and kill a victim. PostgreSQL does exactly this.

### Livelock

Everyone keeps reacting to everyone else and nobody progresses. Two workers that both detect a conflict, back off for exactly 100 ms and retry, collide again forever. Fix: **randomized (jittered) backoff**, or a deterministic tiebreaker (lower id wins).

### Starvation

A party waits indefinitely because others keep winning: a writer behind a steady stream of readers on an RW lock, a low-priority Celery queue never consumed because workers always find high-priority tasks, a big tenant's job hogging a shared worker pool. Fix: fairness (FIFO locks, aging priorities), dedicated capacity per class of work, and per-tenant concurrency limits.

## In practice

### Application-level lock ordering

```python
def transfer(src, dst, amount):
    first, second = sorted([src, dst], key=lambda a: a.id)   # global order
    with first.lock, second.lock:
        src.balance -= amount
        dst.balance += amount
```

Lock with a timeout so a bug becomes an error, not a hang:

```python
if not lock.acquire(timeout=2):
    raise TimeoutError("could not acquire lock for tenant rollup")
try:
    ...
finally:
    lock.release()
```

### PostgreSQL deadlocks

Postgres waits `deadlock_timeout` (default 1 s) before checking the wait-for graph. If it finds a cycle, it aborts one transaction:

```text
ERROR:  deadlock detected
DETAIL:  Process 4121 waits for ShareLock on transaction 99812; blocked by process 4188.
         Process 4188 waits for ShareLock on transaction 99811; blocked by process 4121.
HINT:  See server log for query details.
CONTEXT: while updating tuple (12,7) in relation "inventory"
```

The classic Django cause: two requests update the same rows in different orders.

```python
# Request A: order with items [sku 5, sku 9]
# Request B: order with items [sku 9, sku 5]
with transaction.atomic():
    for item in order.items:
        Inventory.objects.filter(sku=item.sku).update(qty=F("qty") - item.qty)
```

Fix by ordering, and lock everything you need up front:

```python
with transaction.atomic():
    skus = sorted(i.sku for i in order.items)
    rows = {
        r.sku: r
        for r in Inventory.objects.select_for_update().filter(sku__in=skus).order_by("sku")
    }
    for item in order.items:
        rows[item.sku].qty -= item.qty
    Inventory.objects.bulk_update(rows.values(), ["qty"])
```

`SELECT ... FOR UPDATE ... ORDER BY sku` acquires row locks in sorted order, so concurrent transactions queue instead of crossing.

Other common Postgres deadlock sources:

- **Bulk `UPDATE ... WHERE id IN (...)`** without a deterministic order. Postgres locks rows in scan order, which can differ between plans. Lock first with `SELECT ... FOR UPDATE ORDER BY id`.
- **Foreign keys**: inserting a child takes `FOR KEY SHARE` on the parent row; concurrent updates to the parent plus child inserts in different orders can cycle.
- **Upserts on unique indexes** with batches in different orders. Sort the batch before `INSERT ... ON CONFLICT`.
- **Long transactions** that touch many tables: a Celery task that wraps a whole import in one `atomic()`.

### Diagnose

```sql
-- who is blocking whom right now
SELECT pid, pg_blocking_pids(pid) AS blocked_by, state,
       now() - xact_start AS xact_age, left(query, 80) AS query
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0;

-- deadlocks since stats reset, per database
SELECT datname, deadlocks FROM pg_stat_database;
```

```text
# postgresql.conf
log_lock_waits = on          # log waits longer than deadlock_timeout
deadlock_timeout = 1s
```

The server log contains both queries in the cycle; that is where you find the two code paths.

### Timeouts as a safety net

```sql
SET lock_timeout = '3s';                          -- fail fast instead of queueing
SET statement_timeout = '30s';
SET idle_in_transaction_session_timeout = '60s';  -- kill forgotten open transactions
```

In Django, set these per connection with `OPTIONS: {"options": "-c lock_timeout=3000 -c statement_timeout=30000"}`, or per transaction with `SET LOCAL`. For migrations on busy tables, a short `lock_timeout` stops an `ALTER TABLE` from queueing behind a long query and blocking every other query behind it.

### Retry the victim

A deadlock victim is safe to retry: its transaction was rolled back completely.

```python
import random, time
from django.db import OperationalError, transaction
from psycopg import errors

def with_deadlock_retry(fn, attempts=3):
    for i in range(attempts):
        try:
            with transaction.atomic():
                return fn()
        except OperationalError as e:
            if not isinstance(e.__cause__, errors.DeadlockDetected) or i == attempts - 1:
                raise
            time.sleep(random.uniform(0.05, 0.2) * (2 ** i))   # jitter avoids livelock
```

Retries are a mitigation. If deadlocks are frequent, fix the ordering.

## Pitfalls

- **Treating retries as the fix.** Under load, retries add more contention.
- **Non-deterministic lock order** hidden in ORM code: iterating a set, `bulk_update` on unsorted lists, signals that update related rows.
- **Holding locks across network calls**: calling Stripe or an LLM inside `transaction.atomic()` with rows locked.
- **Celery task calling `.get()` on another task** from inside a worker: with all workers waiting on children that can't be scheduled, the pool deadlocks. Use chains/chords instead.
- **Thread pool starvation deadlock**: tasks in a pool submit subtasks to the same bounded pool and wait for them.
- **Same backoff for every retrier**: livelock. Add jitter.
- **Idle-in-transaction sessions** holding locks for minutes after an exception path forgot to roll back.

## Checklist

- Lock resources in one global order (usually by primary key).
- Lock up front with `select_for_update().order_by("id")`.
- Keep transactions short; no external I/O inside them.
- Set `lock_timeout`, `statement_timeout`, `idle_in_transaction_session_timeout`.
- Enable `log_lock_waits` and alert on `pg_stat_database.deadlocks` increasing.
- Retry deadlock victims with jittered backoff, and still fix the root cause.

## Interview angle

- **"What are the conditions for deadlock?"** Mutual exclusion, hold and wait, no preemption, circular wait. Remove any one, most practically circular wait via lock ordering.
- **"Deadlock vs livelock vs starvation?"** Deadlock: everyone blocked waiting in a cycle. Livelock: everyone active but repeatedly undoing each other. Starvation: the system progresses but one party never gets the resource.
- **"How does Postgres handle deadlocks?"** After `deadlock_timeout` a waiting backend checks the wait-for graph and aborts one transaction with `deadlock detected`; the application should retry it and fix the inconsistent lock ordering.
