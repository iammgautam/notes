---
title: Zero-downtime migrations
section: postgresql
order: 16
summary: Change a live schema without blocking traffic, covering lock-safe DDL, concurrent indexes, NOT NULL and defaults, batched backfills, and expand-contract.
---

Schema changes cause outages in two ways: a statement holds a strong lock for a long time (rewriting a table, building an index), or a statement that would be fast waits for a lock and blocks everything queued behind it. Zero-downtime migrations means choosing DDL that only needs brief locks, always setting a lock timeout, and splitting changes so that old and new application code both work during a deploy.

## How it works

Most `ALTER TABLE` forms take `ACCESS EXCLUSIVE`, which conflicts with every query including `SELECT`. What matters is how long it is held:

| Operation | Lock | Duration on a big table |
|---|---|---|
| `ADD COLUMN` nullable, no default | `ACCESS EXCLUSIVE` | Instant (catalog only) |
| `ADD COLUMN ... DEFAULT <constant>` (PG 11+) | `ACCESS EXCLUSIVE` | Instant (default stored in catalog) |
| `ADD COLUMN ... DEFAULT now()` / `gen_random_uuid()` | `ACCESS EXCLUSIVE` | Instant for `now()` (stable); **rewrite** for volatile defaults |
| `ADD COLUMN ... GENERATED ... STORED` | `ACCESS EXCLUSIVE` | **Rewrite** |
| `ALTER COLUMN TYPE` (most changes) | `ACCESS EXCLUSIVE` | **Rewrite** + index rebuilds |
| `ALTER COLUMN TYPE varchar(50)` to `varchar(100)` or `text` | `ACCESS EXCLUSIVE` | Instant |
| `ALTER COLUMN SET NOT NULL` | `ACCESS EXCLUSIVE` | **Full scan**, unless a valid `CHECK (col IS NOT NULL)` exists (PG 12+) |
| `ADD CONSTRAINT ... CHECK` | `ACCESS EXCLUSIVE` | Full scan; use `NOT VALID` |
| `ADD FOREIGN KEY` | `SHARE ROW EXCLUSIVE` both tables | Full scan; use `NOT VALID` |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` | Scan, but reads and writes continue |
| `CREATE INDEX` | `SHARE` | Blocks writes for the whole build |
| `CREATE INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | Slower, writes continue |
| `DROP COLUMN` | `ACCESS EXCLUSIVE` | Instant (marks column dropped) |
| `RENAME COLUMN` / `RENAME TABLE` | `ACCESS EXCLUSIVE` | Instant, but breaks running code |

"Instant" still means acquiring `ACCESS EXCLUSIVE`, which waits behind any running query on the table, and everything else then waits behind it. That's why every migration needs `lock_timeout`.

## In practice

### Always set timeouts

```sql
SET lock_timeout = '3s';
SET statement_timeout = '0';   -- for long but non-blocking ops like CIC or VALIDATE
```

If the lock isn't acquired in 3 seconds, the statement fails and the queue behind it drains. Retry a few times with backoff. In Django, run `SET lock_timeout` via `migrations.RunSQL` at the start of the migration, or set it on the migration database role:

```sql
ALTER ROLE migrator SET lock_timeout = '3s';
```

### Indexes

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS orders_tenant_created_idx
  ON orders (tenant_id, created_at);
```

- Can't run inside a transaction block. In Django, set `atomic = False` on the migration and use `AddIndexConcurrently` from `django.contrib.postgres.operations`.
- Waits, in several phases, for transactions that could see the table to finish. A long-running transaction stalls it.
- If it fails, it leaves an `INVALID` index that still costs writes. Find and drop it:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
DROP INDEX CONCURRENTLY orders_tenant_created_idx;
```

Unique constraints: build the unique index concurrently, then attach it:

```sql
CREATE UNIQUE INDEX CONCURRENTLY projects_tenant_slug_uidx ON projects (tenant_id, slug);
ALTER TABLE projects ADD CONSTRAINT projects_tenant_slug_key
  UNIQUE USING INDEX projects_tenant_slug_uidx;
```

### Adding NOT NULL to an existing column

```sql
-- 1. Instant: add constraint without checking existing rows
ALTER TABLE orders ADD CONSTRAINT orders_currency_nn
  CHECK (currency IS NOT NULL) NOT VALID;

-- 2. Backfill nulls in batches (see below)

-- 3. Scan without blocking writes
ALTER TABLE orders VALIDATE CONSTRAINT orders_currency_nn;

-- 4. Instant: Postgres uses the valid CHECK to skip the scan
ALTER TABLE orders ALTER COLUMN currency SET NOT NULL;

-- 5. Optional cleanup
ALTER TABLE orders DROP CONSTRAINT orders_currency_nn;
```

### Foreign keys

```sql
ALTER TABLE invoices ADD CONSTRAINT invoices_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers(id) NOT VALID;
ALTER TABLE invoices VALIDATE CONSTRAINT invoices_customer_fk;
```

Index the referencing column (`customer_id`) first, or deletes on `customers` will scan `invoices`.

### Backfills

Never `UPDATE big_table SET new_col = ...` in one statement: one huge transaction, massive WAL, bloat, replication lag, and a held xmin horizon. Batch by primary key:

```sql
UPDATE orders
SET currency = 'USD'
WHERE id IN (
  SELECT id FROM orders
  WHERE currency IS NULL AND id > $last_id
  ORDER BY id LIMIT 5000
)
RETURNING id;
```

Run it from a management command or a job, commit each batch, sleep briefly between batches, and watch replication lag. Track progress by the last id rather than re-scanning from the start.

### Expand and contract

Any change that old code can't tolerate gets split across deploys. Renaming `orders.amount` to `orders.total_cents`:

```text
1. Expand    add total_cents (nullable)                     migration
2. Dual-write app writes both columns                        deploy
3. Backfill  copy amount -> total_cents in batches           job
4. Switch    app reads total_cents; still writes both        deploy
5. Stop      app stops using amount                          deploy
6. Contract  drop amount                                     migration
```

At every step, the currently running code and the code about to be deployed both work with the current schema. Rollback is possible until step 6.

For Django, removing a field from the model and dropping the column in the same deploy breaks old pods still selecting it. Use `SeparateDatabaseAndState` to remove the field from state first, and drop the column in a later release.

### Changing a column type

`int` to `bigint` on a large table is a rewrite. Expand-contract instead: add `id_new bigint`, keep it in sync with a trigger, backfill in batches, build a unique index concurrently, then swap in a short transaction with `lock_timeout`.

## Pitfalls

- **No `lock_timeout`.** A two-millisecond `ALTER` queues behind a 10-minute report and takes the API down.
- **Django's default migration transaction.** Several DDL statements in one atomic migration hold `ACCESS EXCLUSIVE` locks until the last one finishes. Split risky operations into separate, non-atomic migrations.
- **Volatile defaults.** `DEFAULT gen_random_uuid()` or `DEFAULT clock_timestamp()` rewrites the table. Add without default, set the default afterward (applies to new rows only), backfill.
- **Django `AddField` with a default.** Django adds the column with the default, then drops the DB default; on PG 11+ this is fast for constants. Django 5.0's `db_default` keeps a real database default.
- **`CREATE INDEX CONCURRENTLY` hanging.** It waits for old transactions; check `pg_stat_activity` for long-running ones.
- **Data migrations in the schema migration.** A `RunPython` backfill inside the deploy's migration step holds locks and blocks the deploy. Run backfills as separate jobs.
- **Renames.** Never rename a column or table in a single step on a live system.

## Checklist

- `lock_timeout` on every migration; retry on timeout.
- `CREATE INDEX CONCURRENTLY` / `AddIndexConcurrently`, non-atomic migration.
- Constraints and FKs: `NOT VALID`, then `VALIDATE`.
- `NOT NULL` via validated `CHECK`, then `SET NOT NULL`.
- Batched backfills outside the deploy.
- Expand, migrate, contract for renames, type changes and removals.
- Check for `INVALID` indexes after failures.

## Interview angle

**Q: How do you add an index to a 500 GB table without downtime?**
`CREATE INDEX CONCURRENTLY` with `lock_timeout`, outside a transaction, then verify it's valid. It takes longer but doesn't block writes.

**Q: How do you add a `NOT NULL` column with a default?**
On PG 11+, a constant default is instant. For an existing column, add `CHECK (col IS NOT NULL) NOT VALID`, backfill, `VALIDATE`, then `SET NOT NULL`, which skips the scan.

**Q: How do you rename a column safely?**
Expand-contract: add the new column, dual-write, backfill, switch reads, stop writing the old one, then drop it in a later release.
