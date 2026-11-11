---
title: MVCC and vacuum
section: postgresql
order: 6
summary: How Postgres keeps multiple row versions, why that causes bloat, how to tune autovacuum, and how to stay far away from transaction ID wraparound.
---

Postgres never updates a row in place. Every `UPDATE` writes a new version and leaves the old one behind for any transaction that might still need it. This is what lets readers and writers not block each other, and it is also why vacuum exists, why tables bloat, and why a forgotten idle transaction can degrade an entire database.

## How it works

### Tuple versions

Each heap tuple carries hidden system columns:

- `xmin`: the transaction ID (xid) that created this version.
- `xmax`: the xid that deleted or updated it (0 if live), or that locked it.
- `ctid`: physical location `(page, offset)`; for an updated row, points to the newer version.

```text
UPDATE jobs SET status = 'running' WHERE id = 7;   -- by xid 1001

page 12: (xmin=900,  xmax=1001, status='pending')  -- old version, dead after commit
page 12: (xmin=1001, xmax=0,    status='running')  -- new version
```

A `DELETE` just sets `xmax`. An `INSERT` creates a tuple with `xmin` set.

### Snapshots and visibility

Each statement (in `READ COMMITTED`) or transaction (in `REPEATABLE READ` and above) takes a **snapshot**: the set of xids that were committed at that moment. A tuple is visible if its `xmin` committed before the snapshot and its `xmax` did not. Commit status is looked up in the commit log (`pg_xact`) and cached on the tuple as **hint bits**, which is why a plain `SELECT` can dirty pages right after a bulk load.

### Dead tuples and vacuum

After the updating transaction commits and **no running snapshot can see the old version**, it becomes a dead tuple. Space is reclaimed only when vacuum runs. `VACUUM`:

1. Scans heap pages (skipping all-visible pages via the visibility map).
2. Removes index entries pointing to dead tuples.
3. Marks the heap space as reusable in the **free space map**.
4. Sets visibility-map bits on pages where everything is visible to everyone (enables index-only scans).
5. **Freezes** old tuples (see wraparound below).

Plain `VACUUM` does not shrink the file; it makes space reusable. `VACUUM FULL` rewrites the table but takes an `ACCESS EXCLUSIVE` lock for the duration, blocking reads and writes. Use `pg_repack` or `pg_squeeze` for online compaction.

### HOT updates

If an update changes no indexed column and the new version fits on the same page, Postgres makes a **heap-only tuple**: no index entries are written, and the old version can be pruned during normal page access without vacuum. HOT is the biggest lever for update-heavy tables. Lower `fillfactor` (for example 80–90) to leave room on each page.

### The xmin horizon

Vacuum can only remove tuples older than the oldest snapshot still in use anywhere in the cluster. That horizon is held back by:

- Long-running queries and transactions (including `idle in transaction`).
- Replication slots that aren't consumed (logical slots especially).
- Standbys with `hot_standby_feedback = on` running long queries.
- Abandoned prepared transactions (`pg_prepared_xacts`).

One analytics query running for 3 hours on the primary prevents vacuum from cleaning the jobs table that churns 5,000 rows per second. That's 54M dead tuples.

## Autovacuum

Autovacuum launches workers that vacuum a table when:

```text
dead_tuples > autovacuum_vacuum_threshold
            + autovacuum_vacuum_scale_factor * reltuples
```

Defaults are 50 + 20% of the table. On a 500M-row table, that's 100M dead tuples before vacuum starts, which is far too late. Since PG 13, inserts also trigger vacuum (`autovacuum_vacuum_insert_scale_factor`, default 20%) so append-only tables get frozen and visibility-mapped.

Key settings:

| Setting | Default | Typical production change |
|---|---|---|
| `autovacuum_max_workers` | 3 | 5–10 on big clusters |
| `autovacuum_vacuum_scale_factor` | 0.2 | 0.01–0.05 per big table |
| `autovacuum_vacuum_cost_limit` | -1 (uses 200) | 1000–4000 on SSD |
| `autovacuum_vacuum_cost_delay` | 2ms | 2ms or less |
| `autovacuum_naptime` | 1min | fine |
| `maintenance_work_mem` / `autovacuum_work_mem` | 64MB | 1GB (dead TID storage) |

The cost limit is shared across all workers, so adding workers without raising the limit makes each one slower. PG 17 replaced the dead-TID array with a radix-tree store that uses far less memory and removes the old 1 GB cap.

Per-table tuning for hot tables:

```sql
ALTER TABLE jobs SET (
  autovacuum_vacuum_scale_factor = 0.01,
  autovacuum_vacuum_threshold = 1000,
  autovacuum_vacuum_cost_limit = 2000,
  fillfactor = 85
);
```

## Transaction ID wraparound

Xids are 32-bit, and comparisons are modulo 2^32: about 2 billion xids are "in the past" and 2 billion "in the future". Tuples older than ~2 billion transactions would suddenly look like they're from the future and vanish. To prevent this, vacuum **freezes** old tuples, marking them as visible to all regardless of xid.

- `autovacuum_freeze_max_age` (200M): when a table's `relfrozenxid` age exceeds this, an **anti-wraparound** autovacuum runs. It cannot be skipped and does not yield to lock requests like normal autovacuum does.
- Within a few million xids of the 2 billion limit, Postgres stops assigning new xids and refuses writes until you vacuum.
- Multixact IDs (used for shared row locks and FK checks) have the same problem with `autovacuum_multixact_freeze_max_age`.

Monitor it:

```sql
SELECT datname, age(datfrozenxid) AS xid_age,
       round(100.0 * age(datfrozenxid) / 2000000000, 1) AS pct_to_wrap
FROM pg_database ORDER BY 2 DESC;

SELECT c.oid::regclass AS table, age(c.relfrozenxid) AS xid_age,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
FROM pg_class c
WHERE c.relkind IN ('r', 'm', 't')
ORDER BY age(c.relfrozenxid) DESC LIMIT 10;
```

> **In production:** Alert when `age(datfrozenxid)` passes 500M–1B. At that point you still have time to find what's blocking freezing (usually a stuck transaction or slot) and run a manual `VACUUM (FREEZE, VERBOSE)`.

## In practice

Find tables with the most dead tuples and when they were last vacuumed:

```sql
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum, autovacuum_count
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC LIMIT 15;
```

Find what holds back the xmin horizon:

```sql
SELECT pid, state, now() - xact_start AS xact_age, backend_xmin, left(query, 60)
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY age(backend_xmin) DESC LIMIT 5;

SELECT slot_name, active, xmin, catalog_xmin,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag
FROM pg_replication_slots;
```

Protect yourself from forgotten transactions:

```sql
ALTER ROLE app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE app SET statement_timeout = '30s';
ALTER ROLE reporting SET statement_timeout = '15min';
```

See what autovacuum is doing right now:

```sql
SELECT pid, relid::regclass, phase, heap_blks_scanned, heap_blks_total
FROM pg_stat_progress_vacuum;
```

## Pitfalls

- **Queue tables bloat fastest.** High churn plus `SKIP LOCKED` scans over dead tuples means dequeue latency rises as bloat grows. Aggressive per-table autovacuum and short transactions are mandatory.
- **Idle in transaction.** A Django view that opens `transaction.atomic()` then calls an external API for 30 seconds holds a snapshot that whole time.
- **`VACUUM FULL` in an incident.** It locks the table exclusively; on a hot table that's an outage. Use `pg_repack`.
- **Long-running migrations.** A backfill in one transaction over 50M rows holds the horizon for its whole duration. Batch it.
- **Updating `updated_at` on every read path.** Each touch is a new tuple version; if `updated_at` is indexed, no HOT.
- **Autovacuum canceled by locks.** Regular autovacuum yields to conflicting locks. Frequent DDL or `LOCK TABLE` on a hot table can keep it from ever finishing; check logs with `log_autovacuum_min_duration = 0`.

## Checklist

- Set `idle_in_transaction_session_timeout` for application roles.
- Lower `autovacuum_vacuum_scale_factor` on large or high-churn tables.
- Raise `autovacuum_vacuum_cost_limit` on SSD storage.
- Monitor `n_dead_tup`, `age(datfrozenxid)`, oldest `backend_xmin`, and replication slot lag.
- Use `fillfactor` and avoid indexing frequently updated columns to keep updates HOT.
- Drop unused replication slots.

## Interview angle

**Q: Why does Postgres need vacuum?**
MVCC keeps old row versions for concurrent snapshots. Once no snapshot needs them, vacuum reclaims the space, updates the visibility map, and freezes old xids.

**Q: What is transaction ID wraparound?**
Xids are 32-bit and compared circularly. Unfrozen tuples older than ~2 billion transactions would appear to be in the future. Vacuum freezes tuples to prevent this; if it can't, Postgres stops accepting writes.

**Q: A table keeps bloating despite autovacuum running. Why?**
Usually something holds the xmin horizon: a long transaction, idle-in-transaction session, stale replication slot or standby feedback. Vacuum runs but can't remove tuples still potentially visible.
