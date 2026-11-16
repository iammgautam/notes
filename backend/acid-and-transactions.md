---
title: ACID and transactions
section: backend
order: 15
phase: "Phase 2 · Databases"
summary: What each ACID property means and how databases implement it, isolation levels and anomalies, locking vs optimistic concurrency, retries, Django atomic.
---

Most correctness bugs in a backend are concurrency bugs that only appear under load: two requests read the same balance, both decide there is enough, both write. A transaction makes a group of reads and writes behave as one unit even when the process crashes halfway or a thousand other requests run at once. Knowing exactly what each guarantee covers lets you pick the cheapest correct option instead of wrapping everything in `SERIALIZABLE` or nothing at all.

## ACID properties

ACID is four separate guarantees implemented by different machinery. Interviewers like to ask how the database actually does each one.

### Atomicity

All of a transaction's writes take effect, or none do. Atomicity is about failure, not concurrency: if the connection drops or a constraint fails after three of five statements, the first three are undone.

- **PostgreSQL** never overwrites rows in place. Each write creates a new row version stamped with the transaction ID, and the outcome is recorded in a commit log (`pg_xact`). Rollback just marks the transaction aborted; its versions become invisible and `VACUUM` removes them. See [MVCC and vacuum]({{ '/postgresql/mvcc-and-vacuum/' | relative_url }}).
- **MySQL InnoDB** updates in place and keeps old values in an **undo log**, applied backward on rollback.
- **After a crash**, both replay the write-ahead log; transactions without a commit record count as aborted.

### Consistency

The database moves from one valid state to another, where valid means your invariants hold. The database only enforces invariants you declare (`NOT NULL`, `CHECK`, `UNIQUE`, foreign keys, Postgres exclusion constraints) plus triggers. "Debits equal credits" or "at least one doctor on call" is the application's job, achieved with correct transactions under adequate isolation. `DEFERRABLE INITIALLY DEFERRED` constraints are checked at commit, which helps when a transaction temporarily violates one (swapping two positions in an ordered list).

> **Rule of thumb:** Push every invariant you can into a constraint. A `CHECK (balance >= 0)` or a unique index holds under every isolation level and every code path, including the one someone writes next year.

### Isolation

Concurrent transactions do not see each other's partial work. Full isolation (serializability) means the result equals some one-at-a-time order.

| Technique | How | Used by |
|---|---|---|
| Two-phase locking | Shared locks for reads, exclusive for writes, held to commit | SQL Server default (no snapshot options), MySQL `SERIALIZABLE` |
| MVCC snapshots | Each transaction reads a snapshot; readers never block writers | PostgreSQL, InnoDB, Oracle |
| Serializable snapshot isolation | Snapshots plus read/write dependency tracking; aborts would-be cycles | PostgreSQL `SERIALIZABLE` |

Under MVCC, writers still lock the rows they modify, so two writers to one row serialize on that lock.

### Durability

Once the database says "committed", the data survives a crash or power loss. The mechanism is the **write-ahead log (WAL)**:

```text
 client       backend           WAL buffer      WAL file      data files
   |  COMMIT ---> | append commit --> |              |              |
   |              | record            |--- fsync --->|              |
   | <---- OK ----|  (durable now)    |              |              |
   |              |                   |   checkpoint later flushes -->|
```

Changes are appended to the sequential WAL; at commit the WAL is fsynced up to the commit record, and only then does the client hear success. Data pages are written lazily at checkpoints, and crash recovery replays the WAL. **Group commit** shares one fsync among many concurrent commits. `synchronous_commit = off` acknowledges before the flush, risking the last fraction of a second of commits but never corruption, which is acceptable per transaction for low-value writes. `fsync = off` can corrupt the database; never use it in production. Disks whose volatile write caches acknowledge early break durability unless they have power-loss protection. And one machine is not durable against a dead disk, which is why durability in practice includes [replication]({{ '/backend/replication/' | relative_url }}).

## Database transactions

A transaction is `BEGIN`, statements, then `COMMIT` or `ROLLBACK`. Without `BEGIN`, drivers and Django run in **autocommit**: each statement is its own transaction. **Savepoints** undo part of a transaction without abandoning it.

### Isolation levels and anomalies

- **Dirty read:** seeing another transaction's uncommitted write.
- **Non-repeatable read:** a row read twice returns different values.
- **Phantom:** a range query re-run returns different rows.
- **Lost update:** two transactions read a value, compute from it, and the second write overwrites the first.
- **Write skew:** two transactions read overlapping data, each checks an invariant, each writes a *different* row, and together they break it. Two on-call doctors each see "two on call" and both go off call.

```text
 time   T1 (withdraw 30)             T2 (withdraw 50)
  |     BEGIN                        BEGIN
  |     SELECT balance -> 100
  |                                  SELECT balance -> 100
  |     UPDATE SET balance = 70
  |     COMMIT
  |                                  UPDATE SET balance = 50
  |                                  COMMIT
  v     final balance 50: the 30 withdrawal is lost (should be 20)
```

What each level allows in PostgreSQL:

| Level | Dirty read | Non-repeatable | Phantom | Lost update | Write skew |
|---|---|---|---|---|---|
| Read uncommitted | No (acts as RC) | Yes | Yes | Yes | Yes |
| Read committed (default) | No | Yes | Yes | Yes | Yes |
| Repeatable read (snapshot) | No | No | No | Error `40001` | Yes |
| Serializable (SSI) | No | No | No | Error `40001` | Error `40001` |

Across engines:

- The SQL standard allows phantoms at repeatable read; Postgres's snapshot-based repeatable read does not.
- **MySQL InnoDB** defaults to repeatable read, but `UPDATE` and locking reads see the latest committed row, so the read-modify-write lost update above is *not* detected. Next-key locks stop phantoms for locking reads.
- **Oracle's** `SERIALIZABLE` is snapshot isolation and allows write skew.
- Defaults: PostgreSQL, Oracle and SQL Server use read committed; MySQL uses repeatable read.

Read committed is safe for single-statement updates (`UPDATE accounts SET balance = balance - 30 WHERE id = 1 AND balance >= 30`): the row lock serializes them and Postgres re-checks the `WHERE` against the newest version after waiting. It is not safe for read-then-write logic in application code. Postgres details are in [transactions and isolation levels]({{ '/postgresql/transactions-and-isolation/' | relative_url }}).

### Optimistic vs pessimistic concurrency

**Pessimistic:** lock what you will change before reading it (`SELECT ... FOR UPDATE`). **Optimistic:** read without locks and make the write conditional on nothing having changed: a version column (`UPDATE ... SET version = version + 1 WHERE id = %s AND version = %s`, then check the row count), HTTP `ETag` plus `If-Match` answering `412`, or serializable isolation, which is optimism run by the database.

| Aspect | Pessimistic (locks) | Optimistic (version check) |
|---|---|---|
| Best when | High contention, expensive retries | Low contention, read-heavy |
| Cost on conflict | Waiting | Retry, or 409/412 to the user |
| Spans user think time | No | Yes |
| Failure modes | Deadlocks, lock queues | Retry storms, starvation |
| Typical use | Transfers, stock decrements, job claiming | Document edits, settings |

### SELECT FOR UPDATE

`SELECT ... FOR UPDATE` locks the returned rows until the transaction ends; other transactions that update, delete or lock them wait, while plain MVCC reads continue.

- **Strengths:** `FOR UPDATE`, `FOR NO KEY UPDATE` (what a plain `UPDATE` takes when keys do not change), `FOR SHARE`, `FOR KEY SHARE` (taken by foreign-key checks). Prefer `FOR NO KEY UPDATE` so child inserts do not block on you.
- **`NOWAIT`** fails at once with `55P03`. **`SKIP LOCKED`** skips locked rows, the standard job-queue pattern ([Postgres as a queue]({{ '/postgresql/postgres-as-a-queue/' | relative_url }})). **`FOR UPDATE OF t`** locks one table in a join.
- **After waiting**, read committed re-checks the `WHERE` on the new version and skips rows that no longer match; repeatable read and serializable raise `40001` instead.
- **Deadlocks** come from locking the same rows in different orders. Postgres aborts one transaction with `40P01` after `deadlock_timeout` (1 s by default). Always lock in a fixed order such as `ORDER BY id`. See [locking]({{ '/postgresql/locking/' | relative_url }}).

It cannot lock rows that do not exist yet. For "no two bookings overlap", lock a parent row that always exists (the room), add an exclusion constraint (`EXCLUDE USING gist (room_id WITH =, during WITH &&)`), or use serializable.

### Serialization failures and retries

Postgres aborts rather than allow an anomaly: `40001 serialization_failure` under repeatable read and serializable, and `40P01 deadlock_detected` at any level. Both mean nothing was committed. To retry correctly:

1. **Rerun the whole transaction from `BEGIN`**, re-reading everything; earlier reads are stale.
2. **Bound attempts** (3 to 5) with exponential backoff and jitter ([retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }})).
3. **Keep side effects out** of the retried block, or make them idempotent.
4. **Treat some failures as normal.** SSI tracks dependencies coarsely and aborts some transactions that would not truly conflict. Short transactions lower the rate.

### Django transaction.atomic

Django runs in autocommit by default.

- **Outermost `atomic`** issues `BEGIN`, commits on normal exit, rolls back on exception. **Nested** blocks are savepoints; `savepoint=False` skips them.
- **`durable=True`** (Django 3.2+) errors if nested, guaranteeing a commit when the block exits.
- **`transaction.on_commit(fn)`** runs after the outermost commit and is dropped on rollback. Enqueue Celery tasks and send emails there.
- **`ATOMIC_REQUESTS = True`** wraps each view in a transaction, holding it open during slow rendering or HTTP calls; opt out with `@transaction.non_atomic_requests`.
- **Swallowing a database error** inside `atomic` makes the next query raise `TransactionManagementError`. Wrap the risky statement in a nested `atomic` and catch outside it.
- **`select_for_update()`** must run inside `atomic`; it accepts `nowait`, `skip_locked`, `of` and `no_key`.

## In practice

### Pessimistic transfer and conditional updates (illustrative Django)

```python
from django.db import transaction
from django.db.models import F


def transfer(src_id, dst_id, amount):
    with transaction.atomic(durable=True):
        rows = (Account.objects.select_for_update(no_key=True)
                .filter(id__in=[src_id, dst_id]).order_by("id"))  # fixed order
        accounts = {acct.id: acct for acct in rows}
        src, dst = accounts[src_id], accounts[dst_id]
        if src.balance < amount:
            raise InsufficientFunds(src_id)
        src.balance -= amount
        dst.balance += amount
        src.save(update_fields=["balance"])
        dst.save(update_fields=["balance"])
        transaction.on_commit(lambda: notify_transfer.delay(src_id, dst_id))


# Often better: one conditional UPDATE, no read-modify-write at all
if not Account.objects.filter(id=acct_id, balance__gte=amount).update(
        balance=F("balance") - amount):
    raise InsufficientFunds(acct_id)

# Optimistic version check for edits that span user think time
if not Document.objects.filter(id=doc_id, version=client_version).update(
        body=new_body, version=F("version") + 1):
    raise Conflict("document changed; reload")   # HTTP 409 or 412
```

### Retrying serializable transactions (illustrative Django)

```python
import random
import time
from functools import wraps

from django.db import DatabaseError, connection, transaction

RETRYABLE = {"40001", "40P01"}


def _sqlstate(exc):
    cause = exc.__cause__        # driver error: psycopg 3 or psycopg2
    return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)


def serializable(attempts=5, base_delay=0.02):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    with transaction.atomic(durable=True):
                        with connection.cursor() as cur:
                            cur.execute(
                                "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                        return fn(*args, **kwargs)
                except DatabaseError as exc:
                    if (_sqlstate(exc) not in RETRYABLE
                            or attempt == attempts - 1):
                        raise
                    time.sleep(random.uniform(0, base_delay * 2 ** attempt))
        return wrapper
    return decorator


@serializable()
def go_off_call(doctor_id, shift_id):
    if Doctor.objects.filter(shift_id=shift_id, on_call=True).count() < 2:
        raise MustKeepOneOnCall()
    Doctor.objects.filter(id=doctor_id).update(on_call=False)
```

`SET TRANSACTION` works here because it is the first statement of the transaction. For a database-wide default, set `"isolation_level"` in the alias's `OPTIONS` (with psycopg 3, `psycopg.IsolationLevel.SERIALIZABLE`).

### Atomicity, lost updates and optimistic retries (runnable)

Stdlib `sqlite3`: a failed transfer rolls back cleanly, a blind read-modify-write loses an update, and a version check catches the same race and retries. SQLite locks the whole database for writes, so this models the logic, not Postgres row locking.

```python
import os
import sqlite3
import tempfile


def connect(path):
    # isolation_level=None: no implicit transactions, we issue BEGIN ourselves
    return sqlite3.connect(path, isolation_level=None, timeout=5)


def transfer(conn, src, dst, amount):
    """Atomicity: both updates commit, or neither does."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?",
                     (amount, dst))
        conn.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?",
                     (amount, src))      # CHECK (balance >= 0) may fail here
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")         # undoes the credit too
        raise


def withdraw(conn, acct, amount, attempts=3, on_read=None):
    """Optimistic concurrency: compare-and-set on a version column."""
    for _ in range(attempts):
        balance, version = conn.execute(
            "SELECT balance, version FROM accounts WHERE id = ?", (acct,)
        ).fetchone()
        if balance < amount:
            raise ValueError("insufficient funds")
        if on_read:
            on_read()                    # test hook: a concurrent writer
            on_read = None
        cur = conn.execute(
            "UPDATE accounts SET balance = ?, version = version + 1 "
            "WHERE id = ? AND version = ?",
            (balance - amount, acct, version),
        )
        if cur.rowcount == 1:            # nobody changed it since our read
            return balance - amount
    raise RuntimeError("too much contention; report a conflict")


def balance(conn, acct):
    return conn.execute(
        "SELECT balance FROM accounts WHERE id = ?", (acct,)).fetchone()[0]


if __name__ == "__main__":
    path = os.path.join(tempfile.mkdtemp(), "bank.db")
    a, b = connect(path), connect(path)
    a.executescript("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL CHECK (balance >= 0),
            version INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO accounts (id, balance) VALUES (1, 100), (2, 50);
    """)

    transfer(a, 1, 2, 30)
    try:
        transfer(a, 1, 2, 500)           # overdraft: debit fails
        raise AssertionError("expected CHECK violation")
    except sqlite3.IntegrityError:
        pass
    assert (balance(a, 1), balance(a, 2)) == (70, 80)   # credit rolled back

    x, y = balance(a, 1), balance(b, 1)  # two blind read-modify-writes
    a.execute("UPDATE accounts SET balance = ? WHERE id = 1", (x - 10,))
    b.execute("UPDATE accounts SET balance = ? WHERE id = 1", (y - 10,))
    assert balance(a, 1) == 60           # lost update: should be 50

    # b withdraws between a's read and a's write; a's CAS fails, it retries
    left = withdraw(a, 1, 10, on_read=lambda: withdraw(b, 1, 10))
    assert left == 40 and balance(a, 1) == 40
    print("acid demo ok")
```

## Pitfalls

- **Read-modify-write in Python.** `obj.balance -= 10; obj.save()` loses updates under concurrency, and a plain `save()` also overwrites columns you never touched. Use `F()`, a conditional `UPDATE`, `select_for_update`, a version check, and `update_fields`.
- **Long transactions.** Holding a transaction across an HTTP or LLM call keeps row locks, blocks `VACUUM` and pins a pooled connection. Symptom: `idle in transaction` sessions in `pg_stat_activity`. Set `idle_in_transaction_session_timeout` and move slow work out.
- **Side effects inside the transaction.** A task enqueued inside `atomic` can run before commit or after a rollback. Use `on_commit`, or an outbox table for guaranteed delivery.
- **Inconsistent lock order.** `40P01` under load usually means two code paths lock the same rows in different orders. Sort IDs before locking.
- **Assuming repeatable read stops write skew.** It does not in Postgres, MySQL or Oracle. Use a constraint, a parent-row lock, or serializable.
- **Retrying without idempotency.** A retried transaction that already charged a card charges twice. Keep external calls outside, or use an [idempotency]({{ '/backend/idempotency/' | relative_url }}) key.

## Interview questions

**How is each ACID property implemented?**
Atomicity: MVCC versions plus a commit log (Postgres) or undo logs (InnoDB), with WAL replay after crashes. Consistency: declared constraints plus correct transactions. Isolation: MVCC snapshots, row locks, SSI or 2PL. Durability: WAL fsync at commit, plus replication against machine loss.

**Lost update versus write skew?**
A lost update is two transactions writing the *same* row from a stale read. Write skew is two transactions writing *different* rows from an overlapping read, jointly breaking an invariant. Postgres snapshot isolation stops the first, not the second.

**Is Postgres's default isolation enough?**
Read committed is enough for single-statement updates and for reads that do not feed decisions. Check-then-act logic needs row locks, conditional updates, constraints or serializable.

**Optimistic or pessimistic locking for seat booking?**
Constraint-based or pessimistic: a unique constraint on (event, seat), or `FOR UPDATE SKIP LOCKED` to claim seats, since contention on popular events is high and optimistic retries would storm. Optimistic fits low-contention edits.

**How do you handle serialization failures?**
Catch `40001` and `40P01`, roll back, rerun the whole transaction with bounded, jittered retries, and keep side effects outside or idempotent.

**Why `transaction.on_commit`?**
Work triggered by a write must happen only if it commits and only once it is visible. `on_commit` runs after the outermost commit and is discarded on rollback.

**How is a commit durable without writing data pages?**
The WAL is fsynced up to the commit record; pages are flushed at checkpoints and recovery replays the WAL. Group commit amortizes the fsync.

## Further reading

- *Designing Data-Intensive Applications*, Martin Kleppmann, chapter 7 "Transactions".
- "A Critique of ANSI SQL Isolation Levels", Berenson et al., SIGMOD 1995.
- "Serializable Snapshot Isolation in PostgreSQL", Ports and Grittner, VLDB 2012.
- PostgreSQL documentation, "Concurrency Control".
- Django documentation, "Database transactions".
