---
title: Locking
section: postgresql
order: 8
summary: Row and table lock modes, which DDL takes which lock, FOR UPDATE / SKIP LOCKED / NOWAIT, deadlocks, advisory locks, and debugging with pg_locks.
---

MVCC means reads rarely block, but locks still decide who waits for whom on writes and DDL. Most lock-related outages aren't deadlocks; they're a harmless-looking `ALTER TABLE` queued behind a long query, with every subsequent request on that table queued behind the `ALTER`. Understanding lock modes and the lock queue is how you avoid that.

## How it works

### Table-level lock modes

Every statement takes a table-level lock. Modes, weakest to strongest:

| Mode | Taken by |
|---|---|
| `ACCESS SHARE` | `SELECT` |
| `ROW SHARE` | `SELECT ... FOR UPDATE / FOR SHARE` |
| `ROW EXCLUSIVE` | `INSERT`, `UPDATE`, `DELETE`, `MERGE` |
| `SHARE UPDATE EXCLUSIVE` | `VACUUM`, `ANALYZE`, `CREATE INDEX CONCURRENTLY`, `ALTER TABLE VALIDATE CONSTRAINT`, `ALTER TABLE SET (storage params)` |
| `SHARE` | `CREATE INDEX` (non-concurrent) |
| `SHARE ROW EXCLUSIVE` | `CREATE TRIGGER`, `ALTER TABLE ADD FOREIGN KEY` (on both tables) |
| `EXCLUSIVE` | `REFRESH MATERIALIZED VIEW CONCURRENTLY` |
| `ACCESS EXCLUSIVE` | `DROP TABLE`, `TRUNCATE`, most `ALTER TABLE`, `VACUUM FULL`, `REFRESH MATERIALIZED VIEW`, `LOCK TABLE` |

Conflicts you must remember:

- `ACCESS EXCLUSIVE` conflicts with **everything**, including plain `SELECT`.
- `SHARE` (plain `CREATE INDEX`) conflicts with `ROW EXCLUSIVE`: writes block for the whole index build.
- `SHARE UPDATE EXCLUSIVE` conflicts with itself: two `CREATE INDEX CONCURRENTLY` on the same table, or one and autovacuum, serialize.

### The lock queue

Lock requests queue in FIFO order. A request conflicts not only with **held** locks but also with **queued** ones ahead of it.

```text
t0  SELECT (long report)         holds ACCESS SHARE       running
t1  ALTER TABLE ... ADD COLUMN   wants ACCESS EXCLUSIVE   waiting on t0
t2  SELECT (API request)         wants ACCESS SHARE       waiting on t1
t3  SELECT (API request)         wants ACCESS SHARE       waiting on t1
...  connection pool exhausted, site down
```

The `ALTER` itself would take a millisecond. The outage comes from waiting. The fix is `lock_timeout`, covered below and in [Zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).

### Row-level locks

Row locks are stored in the tuple header (`xmax` plus infomask bits), not in shared memory, so locking millions of rows doesn't exhaust a lock table. Shared lockers are tracked with multixact IDs.

| Mode | Taken by | Blocks |
|---|---|---|
| `FOR UPDATE` | `SELECT ... FOR UPDATE`, `DELETE`, `UPDATE` of key columns | All other row locks |
| `FOR NO KEY UPDATE` | Plain `UPDATE` not touching unique/PK columns | `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE` |
| `FOR SHARE` | `SELECT ... FOR SHARE` | Updates |
| `FOR KEY SHARE` | FK checks on the referenced row | Only `FOR UPDATE` (key changes, deletes) |

The `KEY SHARE` / `NO KEY UPDATE` split exists so that inserting a child row (FK check takes `FOR KEY SHARE` on the parent) doesn't block a plain update to the parent.

Row lock waits show up in `pg_locks` as a wait on the **transactionid** of the holder, not on the row.

## In practice

### FOR UPDATE, NOWAIT, SKIP LOCKED

```sql
-- Wait for the lock (default)
SELECT * FROM invoices WHERE id = $1 FOR UPDATE;

-- Fail immediately if locked: SQLSTATE 55P03 lock_not_available
SELECT * FROM invoices WHERE id = $1 FOR UPDATE NOWAIT;

-- Skip rows others hold: the job-queue primitive
SELECT id FROM jobs
WHERE status = 'pending' AND queue = 'email'
ORDER BY run_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

- `NOWAIT` suits user-facing actions where "someone else is editing this" is a better answer than a spinner.
- `SKIP LOCKED` gives an inconsistent view by design; use it for work distribution, not for reads that must be complete.
- `FOR UPDATE OF t` restricts locking to one table in a join.

Django equivalents:

```python
Invoice.objects.select_for_update(nowait=True).get(pk=invoice_id)
Job.objects.select_for_update(skip_locked=True).filter(status="pending")[:10]
Order.objects.select_related("customer").select_for_update(of=("self",))
```

`select_for_update()` must run inside `transaction.atomic()`; Django raises `TransactionManagementError` otherwise.

### Timeouts

```sql
SET lock_timeout = '3s';          -- give up waiting for a lock
SET statement_timeout = '30s';    -- give up running
SET idle_in_transaction_session_timeout = '60s';
```

Set `lock_timeout` on every migration session. A failed migration that you retry is far better than a queued `ACCESS EXCLUSIVE` taking the site down.

### Deadlocks

A deadlock is a cycle: T1 holds row A and wants B, T2 holds B and wants A. After `deadlock_timeout` (1s default) of waiting, Postgres runs the detector and aborts one transaction with SQLSTATE `40P01`.

The fix is consistent lock ordering:

```sql
-- Transfer between accounts: lock in id order regardless of direction
SELECT id FROM accounts WHERE id IN ($from, $to) ORDER BY id FOR UPDATE;
UPDATE accounts SET balance = balance - $amt WHERE id = $from;
UPDATE accounts SET balance = balance + $amt WHERE id = $to;
```

Batch updates that touch many rows in arbitrary order deadlock with each other. Sort by primary key before updating, or lock with `ORDER BY id FOR UPDATE` first.

Set `log_lock_waits = on` to log any wait longer than `deadlock_timeout`; the server log entry for a deadlock includes both queries.

### Advisory locks

Application-defined locks on a 64-bit key, with no table attached. Postgres enforces them; your code decides what they mean.

```sql
-- Session-level: held until explicit unlock or disconnect
SELECT pg_try_advisory_lock(hashtext('rollup:tenant:42'));
SELECT pg_advisory_unlock(hashtext('rollup:tenant:42'));

-- Transaction-level: released at commit/rollback (prefer this)
SELECT pg_advisory_xact_lock(42, 1001);   -- two int4 keys: (namespace, id)
```

Use them for: "only one rollup job per tenant at a time", singleton cron jobs across replicas of a worker, serializing migrations.

> **Gotcha:** Session-level advisory locks and PgBouncer transaction pooling don't mix. The lock stays on a server connection that another client may get next. Use `pg_advisory_xact_lock` in transaction mode.

## Debugging with pg_locks

Who is blocking whom:

```sql
SELECT a.pid,
       pg_blocking_pids(a.pid) AS blocked_by,
       a.wait_event_type, a.wait_event,
       now() - a.query_start AS waiting_for,
       left(a.query, 70) AS query
FROM pg_stat_activity a
WHERE cardinality(pg_blocking_pids(a.pid)) > 0;
```

What locks a table currently has:

```sql
SELECT l.pid, l.mode, l.granted, a.state, left(a.query, 60)
FROM pg_locks l
JOIN pg_stat_activity a USING (pid)
WHERE l.relation = 'orders'::regclass
ORDER BY l.granted DESC;
```

Kill the blocker if it's safe:

```sql
SELECT pg_cancel_backend(12345);     -- cancel current query
SELECT pg_terminate_backend(12345);  -- kill the session
```

## Pitfalls

- **DDL without `lock_timeout`.** The single most common cause of migration outages.
- **Long transactions holding row locks** while doing network calls. Other writers queue; the pool drains.
- **Plain `CREATE INDEX` on a live table** blocks all writes for minutes to hours. Use `CONCURRENTLY`.
- **Adding a foreign key** takes `SHARE ROW EXCLUSIVE` on both tables and scans the child. Add it `NOT VALID`, then `VALIDATE CONSTRAINT` separately.
- **Too many locks in one transaction.** Touching thousands of partitions in one transaction can exceed `max_locks_per_transaction` (`out of shared memory`).
- **Hot-row contention.** A single `tenant_usage` counter row updated per request serializes all requests for that tenant. Shard counters or aggregate asynchronously.

## Checklist

- `lock_timeout` on every migration and DDL session.
- `idle_in_transaction_session_timeout` on application roles.
- `log_lock_waits = on` in production.
- Lock rows in a consistent order.
- Use `SKIP LOCKED` for work queues, `NOWAIT` for user-facing edits.
- Prefer transaction-scoped advisory locks.

## Interview angle

**Q: Why did a quick `ALTER TABLE` take the site down?**
It needed `ACCESS EXCLUSIVE`, queued behind a long-running query, and every new query queued behind it. Use `lock_timeout` and retry.

**Q: How does `SKIP LOCKED` help a job queue?**
Workers skip rows another worker has locked, so many workers can dequeue concurrently without blocking or double-processing.

**Q: How do you prevent deadlocks?**
Acquire locks in a consistent order, keep transactions short, and retry on `40P01`.
