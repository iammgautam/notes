---
title: Database migration strategies
section: backend
order: 17
phase: "Phase 2 · Databases"
summary: Change schemas and move data without downtime, covering migrations as code, safe DDL, expand-contract, batched backfills, dual writes, OSC tools and rollback.
---

Code deploys are easy to undo; schema and data changes are not. A migration that takes a strong lock on a hot table for 30 seconds is an outage, a dropped column is gone, and during every rolling deploy the old and new versions of your code run against the same schema at once. Migration strategy is the discipline of splitting changes into small steps that each keep the system working and can each be stopped or reversed.

## Schema migrations as code

Schema changes live in the repository as versioned migration files, applied in order by a tool that records what has run in a tracking table (`django_migrations`, `alembic_version`, `flyway_schema_history`). The same files build every environment, so production never drifts from what was reviewed.

| Approach | How | Tools |
|---|---|---|
| Migration-based (imperative) | Ordered change scripts, generated or hand-written | Django, Alembic, Flyway, Liquibase, Rails, golang-migrate |
| State-based (declarative) | Declare the target schema; the tool diffs and plans the change | Atlas, Skeema, sqldef |

Working rules:

- **Review the SQL, not just the Python.** `python manage.py sqlmigrate app 0042` shows what Django will run; the dangerous part is usually invisible in the model diff.
- **Gate in CI.** Fail the build if models changed without a migration (`makemigrations --check --dry-run`), and lint for unsafe operations.
- **Test on production-sized data.** A migration that takes 50 ms on a dev database can take 20 minutes on a 400 GB table.
- **Run migrations once per deploy**, from a CI step, a Kubernetes Job or a Helm pre-upgrade hook, not in every pod's entrypoint. Django does not take a cross-process lock, so parallel `migrate` runs can race. Flyway and Liquibase do lock.
- **Compatibility window.** Every schema must work with both the currently deployed code and the code being rolled out, because a rolling deploy runs both.

## Zero-downtime DDL

The danger is rarely the work itself; it is the lock. Most `ALTER TABLE` forms in PostgreSQL need an `ACCESS EXCLUSIVE` lock, which conflicts even with `SELECT`. If a long query holds a weaker lock, the `ALTER` waits, and every new query on the table queues behind the waiting `ALTER`. A "metadata-only" change becomes an outage. Always set `lock_timeout` (2 to 5 s) and retry on failure.

| Change | Risky form | Safe form (PostgreSQL) |
|---|---|---|
| Add index | `CREATE INDEX` (blocks writes) | `CREATE INDEX CONCURRENTLY`, outside a transaction |
| Add unique constraint | `ADD UNIQUE` | Unique index concurrently, then `ADD CONSTRAINT ... USING INDEX` |
| Add foreign key or check | Validated in one step (full scan under lock) | `ADD ... NOT VALID`, then `VALIDATE CONSTRAINT` |
| Add `NOT NULL` | `SET NOT NULL` (full scan under lock) | `CHECK (col IS NOT NULL) NOT VALID`, validate, then `SET NOT NULL` (PG 12+ uses the check) |
| Add column with default | Volatile default rewrites the table | Constant defaults are instant in PG 11+ |
| Change column type | `ALTER COLUMN TYPE` rewrites | New column, dual write, backfill, switch |
| Rename or drop | Instant, but breaks running code | Expand-contract |

MySQL has the same shape: InnoDB online DDL with `ALGORITHM=INSTANT` or `INPLACE, LOCK=NONE` for supported changes, `COPY` for the rest, and metadata locks that queue exactly like Postgres locks. Large DDL also replays serially on replicas and causes lag, which is why online schema change tools exist. The full Postgres lock table is in [zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).

## Expand-contract

Expand-contract (also called parallel change) turns a breaking change into a sequence of compatible ones. Renaming `users.name` to `users.full_name`:

```text
 step:      1 expand    2 backfill   3 switch     4 stop old   5 contract
 schema:    +full_name  (same)       (same)       (same)       -name
 writes:    both        both         both         full_name    full_name
 reads:     name        name         full_name    full_name    full_name
 rollback:  easy        easy         flag flip    redeploy     backup only
             <---------- old and new code both work ---------->
```

1. **Expand.** Add `full_name` as nullable. Deploy code that writes both columns and still reads `name`.
2. **Backfill.** Copy existing rows in batches (next section).
3. **Switch reads.** Read `full_name`, ideally behind a feature flag so you can flip back instantly. Compare old and new values for a while.
4. **Stop writing the old column.** Deploy code that ignores `name`.
5. **Contract.** After the rollback window has passed and no code reads `name`, drop it.

Every step before the contract is additive, so it can be paused or reversed. The contract step is the point of no return, so it ships last and alone. The same pattern moves a column to another table, splits a table, changes a type, or swaps an enum for a lookup table.

## Backfills in batches

One `UPDATE users SET full_name = name` on 200 million rows is a single long transaction: it locks every touched row against concurrent updates, writes a WAL burst that puts replicas minutes behind, prevents `VACUUM` from cleaning anything newer than its start, and rolls back completely if it fails at 95%.

A safe backfill:

- **Walks the primary key** in ranges (`WHERE id > :last ORDER BY id LIMIT :n`), never `OFFSET`.
- **Commits each batch.** Size batches so each takes roughly 100 ms to 1 s, often 1,000 to 10,000 rows.
- **Is idempotent.** `AND full_name IS NULL` makes a rerun skip finished rows.
- **Is resumable.** It records the last processed ID so a killed job continues where it stopped.
- **Throttles.** It sleeps between batches and pauses while replica lag or database load is high.
- **Runs as a job**, such as a management command or a chain of Celery tasks, not inside the schema migration that runs during the deploy.

The runnable demo in "In practice" does all of this against SQLite.

## Dual writes

During a transition, the application writes both the old and the new shape.

- **Inside one database**, write both columns or tables in the same transaction. Simple and atomic. A database trigger is a strong alternative because it also covers old code still running during the rollout, other services, and manual SQL.
- **Order matters.** Dual writes must be live *before* the backfill starts. Otherwise a row the backfill already copied can be changed by code that writes only the old column, and the new copy is silently stale.
- **Across systems** (two databases, a database and a search index), dual writes are not atomic. A crash between the two writes, a failed second write, or concurrent writes applied in different orders leave the stores inconsistent. Prefer one source of truth plus asynchronous propagation through change data capture or a transactional outbox (see [distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }})), and run a reconciliation job that compares the two.
- **Shadow reads** check the new path before trusting it: read from both, serve the old result, log mismatches, and switch only once mismatches reach zero.

## Online schema change tools

When the database cannot change a big table online, these tools rebuild it beside the original:

```text
 original table ---- copy rows in chunks ----> shadow table (new schema)
       |                                              ^
       +--- binlog (gh-ost) or triggers (pt-osc) -----+
                   replay concurrent changes
 cut-over: brief lock, atomic rename: shadow becomes the original
```

- **gh-ost** (GitHub, MySQL) is triggerless: it tails the row-based binlog, often from a replica, to replay changes onto the shadow table. It throttles on replica lag and load, can be paused, and lets you control when the cut-over happens.
- **pt-online-schema-change** (Percona Toolkit, MySQL) captures changes with triggers on the original table. Simpler, but the triggers add write load and lock interactions.
- **pg_repack** (PostgreSQL) rebuilds a table or its indexes online to remove bloat or re-cluster rows. It logs changes with triggers and needs a brief `ACCESS EXCLUSIVE` lock at the start and end. It is a maintenance tool, not a general `ALTER` replacement.
- Postgres needs this class of tool less often because indexes, constraints and most column additions can already be done online. For type changes, expand-contract is the usual answer; newer tools such as pgroll automate it with versioned views.

## Moving data between databases

Typical cases: extracting a service's tables into its own database, a major version upgrade, moving to a managed service, or re-sharding. The standard pattern is snapshot plus change data capture:

1. **Copy and stream.** Take an initial snapshot and stream changes from that point: Postgres logical replication (`CREATE PUBLICATION` / `CREATE SUBSCRIPTION`), Debezium into Kafka, or AWS DMS.
2. **Verify.** Compare row counts and per-range checksums, and use shadow reads against the new database.
3. **Cut over.** Briefly stop writes (seconds), wait for replication lag to reach zero, copy sequence values (Postgres logical replication has historically not replicated sequences or DDL), switch connection strings, and resume writes.
4. **Keep a way back.** Replicate in reverse, new to old, for a while so you can switch back without losing writes made after the cut-over.

When schemas differ or CDC is unavailable, use application dual writes with a backfill and reconciliation instead. For in-place Postgres major upgrades, `pg_upgrade --link` takes minutes of downtime; logical replication to a new cluster gets that down to seconds. Moving data between shards follows the same steps; see [partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}).

## Rollback strategy

- **Design for code rollback without schema rollback.** If every schema works with the previous release, rolling back code is always safe and the schema can stay.
- **Prefer rolling forward.** Down migrations are fine in development, but in production many cannot be reversed meaningfully: a dropped column's data is gone, and "un-backfilling" means nothing. Fix forward with a new migration.
- **Delay the irreversible step.** Contract only after the rollback window (days, not minutes), and take a snapshot or confirm point-in-time recovery first.
- **Copy, don't move.** Keep old data until the new location is verified.
- **Use flags for read switches** so reverting a read path is a config change, not a deploy.

## Django migrations specifics

- **Transactions.** On PostgreSQL each migration runs in a transaction. For concurrent index builds, set `atomic = False` on the migration and use `AddIndexConcurrently` or `RemoveIndexConcurrently` from `django.contrib.postgres.operations`.
- **Lock timeout.** Put `RunSQL("SET LOCAL lock_timeout = '5s'", migrations.RunSQL.noop)` first in an atomic migration, or set `lock_timeout` on the migration role.
- **Defaults drop out.** `AddField` with `default=` adds the column with that default, fills existing rows, then drops the database default, because Django applies defaults in Python. Old pods that insert without the new column then hit `NOT NULL` violations mid-deploy. Add it as `null=True` first, or use `db_default` (Django 5.0+), which keeps the default in the database.
- **Removing a field takes two releases.** Django names every column in its `SELECT`s, so old pods break the moment a column disappears. First remove the field from the model state only, with `SeparateDatabaseAndState`, after making the column nullable; drop it with `RunSQL` in a later release.
- **Renames.** `RenameField` issues `RENAME COLUMN`, which breaks running code. To rename only in Python, keep the column and set `db_column="old_name"`; to rename in the database, use expand-contract.
- **Data migrations.** `RunPython` must use `apps.get_model()` (historical models) and should pass `reverse_code` (`migrations.RunPython.noop` when there is nothing to undo). Keep large backfills out of migrations.
- **Housekeeping.** `migrate --plan` previews, `showmigrations` shows state, `squashmigrations` compacts history. Third-party linters such as django-migration-linter flag unsafe operations in CI.

## In practice

### Safe NOT NULL and foreign key (illustrative SQL)

```sql
SET lock_timeout = '3s';

ALTER TABLE orders ADD CONSTRAINT orders_tenant_nn
    CHECK (tenant_id IS NOT NULL) NOT VALID;            -- instant
ALTER TABLE orders VALIDATE CONSTRAINT orders_tenant_nn; -- scan, no block
ALTER TABLE orders ALTER COLUMN tenant_id SET NOT NULL;  -- uses the check
ALTER TABLE orders DROP CONSTRAINT orders_tenant_nn;

ALTER TABLE orders ADD CONSTRAINT orders_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES tenants (id) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_tenant_fk;
```

### Concurrent index in Django (illustrative)

```python
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False      # CREATE INDEX CONCURRENTLY can't run in a transaction

    dependencies = [("orders", "0041_order_status")]

    operations = [
        AddIndexConcurrently(
            "order",
            models.Index(fields=["tenant", "created_at"],
                         name="order_tenant_created_idx"),
        ),
    ]
```

If a concurrent build fails it leaves an `INVALID` index behind; drop it and run the migration again.

### Resumable, idempotent backfill (runnable)

Expand with a nullable column, backfill in primary-key batches, survive a killed job, coexist with dual-writing code, and prove a rerun is a no-op.

```python
import sqlite3


def backfill(conn, batch_size, after_id=0, max_batches=None):
    """Fill email_lower in primary-key order, one short transaction per batch.

    Returns (last_id, finished). Safe to rerun: rows already filled are
    skipped, and after_id lets a restarted job resume from its checkpoint.
    """
    batches = 0
    while max_batches is None or batches < max_batches:
        ids = conn.execute(
            "SELECT id FROM users WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, batch_size)).fetchall()
        if not ids:
            return after_id, True
        upper = ids[-1][0]
        with conn:                       # commit per batch, locks held briefly
            conn.execute(
                "UPDATE users SET email_lower = lower(email) "
                "WHERE id > ? AND id <= ? AND email_lower IS NULL",
                (after_id, upper))
        after_id, batches = upper, batches + 1
        # production: sleep, or pause while replica lag is above a threshold
    return after_id, False


def new_code_insert(conn, email):
    """Dual write: the deployed app already fills both columns."""
    with conn:
        conn.execute("INSERT INTO users (email, email_lower) VALUES (?, ?)",
                     (email, email.lower()))


if __name__ == "__main__":
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.executemany("INSERT INTO users (email) VALUES (?)",
                     [(f"User{i}@Example.COM",) for i in range(10_500)])
    conn.commit()

    # Expand: nullable column, no default, so no table rewrite
    conn.execute("ALTER TABLE users ADD COLUMN email_lower TEXT")

    # Job runs three batches, then the pod is killed
    checkpoint, finished = backfill(conn, 1000, max_batches=3)
    assert (checkpoint, finished) == (3000, False)

    # Meanwhile, new code dual-writes and edits an old, unfilled row
    new_code_insert(conn, "Late@Example.COM")
    with conn:
        conn.execute("UPDATE users SET email = ?, email_lower = ? "
                     "WHERE id = 9000", ("Moved@X.IO", "moved@x.io"))

    # Resume from the checkpoint and finish
    _, finished = backfill(conn, 1000, after_id=checkpoint)
    assert finished
    missing = conn.execute(
        "SELECT count(*) FROM users WHERE email_lower IS NULL").fetchone()[0]
    wrong = conn.execute(
        "SELECT count(*) FROM users WHERE email_lower <> lower(email)"
    ).fetchone()[0]
    assert missing == 0 and wrong == 0

    # Rerunning from scratch changes nothing: the backfill is idempotent
    before = conn.total_changes
    backfill(conn, 1000)
    assert conn.total_changes == before
    print("backfill demo ok")
```

## Pitfalls

- **No `lock_timeout`.** A cheap `ALTER` waits behind a long-running report and freezes all traffic on the table. Symptom: a wall of queries waiting on a lock, with the `ALTER` at the head in `pg_stat_activity`.
- **Dropping or renaming while old code runs.** Errors spike during the rollout window. Remove usage first, drop later.
- **Django's dropped default.** A new `NOT NULL` field with `default=` breaks inserts from old pods. Use `null=True` or `db_default`.
- **Backfill inside the deploy.** A `RunPython` over millions of rows holds a transaction for the whole deploy, blocks other migrations and causes replica lag. Run it as a throttled job.
- **Backfill before dual writes.** Rows changed by old code after the backfill passed them stay stale. Turn on dual writes or a trigger first.
- **Migrating in every pod at startup.** Racing runners and half-applied state. Run migrations once per deploy.
- **Trusting dev timings.** Time risky migrations against a production-sized copy.

## Interview questions

**How do you rename a column with zero downtime?**
Expand-contract: add the new column, dual write, backfill in batches, switch reads behind a flag, stop writing the old column, and drop it in a later release. Every step is compatible with both running code versions.

**How do you add a NOT NULL column to a 500-million-row table?**
Add it nullable (or with a constant default, instant in PG 11+), deploy writers, backfill in batches, add `CHECK (col IS NOT NULL) NOT VALID`, validate it, then `SET NOT NULL`, which uses the validated check instead of scanning under lock.

**Why can a "metadata-only" ALTER cause an outage?**
It still needs `ACCESS EXCLUSIVE`. If it waits behind a long query, every new query queues behind it. Set `lock_timeout` and retry.

**How do you backfill hundreds of millions of rows safely?**
PK-ordered batches with a commit each, idempotent predicates, a checkpoint to resume, throttling on replica lag, run as a background job after dual writes are live.

**How do you move a table to a new database with minimal downtime?**
Snapshot plus CDC (logical replication or Debezium), verify with counts and checksums, briefly freeze writes, wait for zero lag, fix sequences, switch connections, and keep reverse replication for rollback.

**Do you rely on down migrations?**
Not in production. Design each schema to support the previous release so code can roll back alone, roll forward for fixes, and delay destructive steps behind a backup.

**How do gh-ost and pt-online-schema-change work?**
Both build a shadow table with the new schema, copy rows in chunks, replay concurrent changes (binlog for gh-ost, triggers for pt-osc), and swap tables with an atomic rename under a brief lock.

## Further reading

- *Refactoring Databases: Evolutionary Database Design*, Scott Ambler and Pramod Sadalage.
- Martin Fowler, "ParallelChange" (bliki).
- PostgreSQL documentation, "ALTER TABLE" and "Explicit Locking".
- gh-ost documentation (github/gh-ost repository).
- Django documentation, "Migrations" and "Writing database migrations".
