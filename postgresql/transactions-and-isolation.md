---
title: Transactions and isolation levels
section: postgresql
order: 7
summary: What each isolation level guarantees in Postgres, which anomalies they allow, and how to handle serialization failures with retries.
---

Isolation levels decide what your transaction sees when others are writing concurrently. Most production bugs here aren't exotic: two requests read the same balance, both decide there's enough, both write. Knowing which level prevents which anomaly, and what it costs you in retries, lets you pick the cheapest correct option instead of reaching for `SERIALIZABLE` everywhere or nothing at all.

## How it works

Postgres implements isolation with MVCC snapshots, not read locks. Readers never block writers and writers never block readers. Writers block writers on the same row.

| Level | Snapshot taken | Postgres behavior |
|---|---|---|
| `READ UNCOMMITTED` | per statement | Same as `READ COMMITTED` in Postgres |
| `READ COMMITTED` (default) | per statement | Each statement sees data committed before it started |
| `REPEATABLE READ` | first statement in txn | Whole transaction sees one snapshot; write conflicts error |
| `SERIALIZABLE` | first statement in txn | Snapshot + Serializable Snapshot Isolation (SSI) conflict tracking |

### Anomalies

| Anomaly | Description | RC | RR | SER |
|---|---|---|---|---|
| Dirty read | See uncommitted data | No | No | No |
| Non-repeatable read | Same row read twice, different values | Yes | No | No |
| Phantom read | Same query twice, new rows appear | Yes | No* | No |
| Lost update | Two read-modify-write cycles, one overwrites the other | Yes | No (errors) | No |
| Write skew | Two txns read overlapping data, write disjoint rows, violate an invariant | Yes | Yes | No |

\* Postgres `REPEATABLE READ` is snapshot isolation and does not show phantoms, which is stronger than the SQL standard requires.

### READ COMMITTED and concurrent updates

In `READ COMMITTED`, when your `UPDATE` finds a row that another transaction has updated but not committed, it waits. After the other commits, Postgres **re-evaluates your `WHERE` clause against the new version** and applies your update to it (the EvalPlanQual recheck). It doesn't error, which is convenient, but it means the rest of your statement may have been computed against an older snapshot.

```sql
-- Both sessions run at the same time, balance starts at 100
UPDATE accounts SET balance = balance - 80 WHERE id = 1 AND balance >= 80;
```

This is safe: the second session waits, re-checks `balance >= 80` against 20, and updates 0 rows. The danger is splitting read and write:

```python
acct = Account.objects.get(pk=1)          # reads 100
if acct.balance >= 80:
    acct.balance -= 80                    # computes 20 in Python
    acct.save()                           # both sessions write 20: lost update
```

### REPEATABLE READ

The transaction uses one snapshot throughout. If you try to update a row that someone else modified and committed after your snapshot, you get:

```text
ERROR:  could not serialize access due to concurrent update
SQLSTATE 40001
```

The transaction must be retried from the start.

### SERIALIZABLE

Adds SSI: Postgres tracks read/write dependencies between concurrent transactions using predicate locks (`SIReadLock` in `pg_locks`, which don't block anything). If it detects a dangerous cycle that could produce a non-serializable result, it aborts one transaction with SQLSTATE `40001`.

The classic write-skew example: an on-call rule requires at least one doctor on call.

```sql
-- Both T1 and T2, concurrently:
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT count(*) FROM on_call WHERE shift_id = 9 AND active;   -- both see 2
UPDATE on_call SET active = false WHERE shift_id = 9 AND doctor_id = :me;
COMMIT;
```

Under `REPEATABLE READ` both commit and nobody is on call. Under `SERIALIZABLE` one gets a serialization failure.

SSI is cheaper than people think, but has costs: more aborts under contention, memory for predicate locks (`max_pred_locks_per_transaction`), and it's only correct if **every** transaction touching the data runs at `SERIALIZABLE`.

## In practice

### Choose the narrowest tool

| Problem | Cheapest fix |
|---|---|
| Counter increment | Atomic `UPDATE ... SET n = n + 1` (Django `F()`) |
| Check-then-act on one row | `SELECT ... FOR UPDATE`, or conditional `UPDATE ... WHERE` |
| Uniqueness | Unique constraint + `INSERT ... ON CONFLICT` |
| Invariant across many rows | `SERIALIZABLE`, or lock a parent row |
| Consistent multi-query report | `REPEATABLE READ` (read-only, no retry needed) |

Atomic update in Django:

```python
from django.db.models import F

updated = (Account.objects
           .filter(pk=1, balance__gte=80)
           .update(balance=F("balance") - 80))
if not updated:
    raise InsufficientFunds()
```

Lock a parent row to serialize writes that maintain an invariant across children:

```python
from django.db import transaction

with transaction.atomic():
    shift = Shift.objects.select_for_update().get(pk=9)
    active = shift.on_call.filter(active=True).count()
    if active <= 1:
        raise MustKeepOneOnCall()
    shift.on_call.filter(doctor_id=me).update(active=False)
```

### Retry serialization failures

Any code using `REPEATABLE READ` or `SERIALIZABLE` must retry on SQLSTATE `40001` (serialization failure) and usually `40P01` (deadlock detected). The whole transaction is retried, including the reads, because the decision was based on stale data.

```python
import random
import time

from django.db import OperationalError, connection, transaction

RETRYABLE = {"40001", "40P01"}

def run_serializable(fn, attempts=5):
    for i in range(attempts):
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                return fn()
        except OperationalError as e:
            code = getattr(e.__cause__, "sqlstate", None) or getattr(e.__cause__, "pgcode", None)
            if code not in RETRYABLE or i == attempts - 1:
                raise
            time.sleep(min(0.5, 0.01 * 2 ** i) * random.random())
```

`SET TRANSACTION` must be the first statement in the transaction; outside `atomic()` blocks you can set `isolation_level` in Django's `DATABASES["default"]["OPTIONS"]`. Django maps serialization failures to `OperationalError`; psycopg 3 exposes `sqlstate`, psycopg2 exposes `pgcode`.

> **Gotcha:** Never retry around code with external side effects (sending email, charging a card, enqueuing to another system) unless those effects are idempotent or deferred with `transaction.on_commit`.

### Read-only consistent reports

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT sum(total_cents) FROM invoices WHERE tenant_id = 42;
SELECT count(*) FROM invoice_lines WHERE tenant_id = 42;
COMMIT;
```

`SERIALIZABLE READ ONLY DEFERRABLE` waits for a safe snapshot and then can never be aborted, good for long reports that must be serializable without slowing writers.

## Pitfalls

- **Read-modify-write in the ORM.** `obj.save()` after reading is a lost update in `READ COMMITTED`. Use `F()`, `select_for_update()`, or conditional updates.
- **Mixed isolation levels.** SSI only protects transactions that run at `SERIALIZABLE`. One `READ COMMITTED` writer on the same table reopens write skew.
- **Long `REPEATABLE READ` transactions** hold the xmin horizon and block vacuum. Keep them short.
- **No retry loop.** Moving to `SERIALIZABLE` without retries turns anomalies into 500s. Measure the abort rate.
- **Retrying only the failing statement.** After `40001`, the transaction is aborted. Retry from `BEGIN`.
- **Savepoints in loops.** Django nested `atomic()` creates savepoints; thousands in one transaction can hit subtransaction overflow (`PGPROC` caches 64 per backend) and slow every snapshot cluster-wide.

> **Rule of thumb:** Default to `READ COMMITTED` and make writes atomic (`UPDATE ... WHERE`, constraints, `FOR UPDATE`). Reach for `SERIALIZABLE` when an invariant spans rows you can't easily lock, and always pair it with retries.

## Interview angle

**Q: What's the default isolation level in Postgres and what can go wrong?**
`READ COMMITTED`. Each statement gets a fresh snapshot, so non-repeatable reads, phantoms, lost updates in read-modify-write code, and write skew are possible.

**Q: Difference between `REPEATABLE READ` and `SERIALIZABLE` in Postgres?**
Both use a transaction-wide snapshot. `SERIALIZABLE` adds SSI dependency tracking and aborts transactions that would produce write skew or other non-serializable outcomes.

**Q: How do you handle serialization failures?**
Catch SQLSTATE `40001`, roll back, and retry the whole transaction with jittered backoff and a cap. Keep side effects out of the retried block.
