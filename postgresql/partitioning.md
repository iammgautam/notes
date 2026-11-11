---
title: Table partitioning
section: postgresql
order: 14
summary: How declarative partitioning works, choosing a partition key, pruning, index and constraint rules, retention with DETACH, and when partitioning doesn't help.
---

Partitioning splits one logical table into many physical tables. It's not a general performance feature; a well-indexed 500M-row table is often faster to query than a badly partitioned one. Partitioning pays off for three things: dropping old data instantly, keeping vacuum and index maintenance bounded per partition, and letting queries that filter on the partition key skip most of the data.

## How it works

Declarative partitioning (PG 10+, mature since 13–14) has three strategies:

| Strategy | Key example | Typical use |
|---|---|---|
| `RANGE` | `created_at` | Time series: events, logs, jobs, audit |
| `LIST` | `region`, `tenant_tier` | Discrete categories |
| `HASH` | `tenant_id` | Spread load evenly, bound per-partition size |

The parent is a routing shell with no storage. Rows live in partitions; inserts are routed by key. Partitions can themselves be partitioned (sub-partitioning).

### Partition pruning

When the `WHERE` clause constrains the partition key, the planner excludes partitions that can't match:

- **Plan-time pruning** with literal values.
- **Execution-time pruning** with parameters, `now()`, or values from a join (shows as `Subplans Removed: N` in `EXPLAIN`).

Pruning only works when the query filters on the partition key in a form the planner can reason about. `WHERE created_at::date = '2026-11-01'` may not prune; `created_at >= '2026-11-01' AND created_at < '2026-11-02'` does.

### Rules you must design around

- **Primary keys and unique constraints must include the partition key.** You can't have a globally unique `id` alone on a table partitioned by `created_at`. The PK becomes `(id, created_at)` or `(tenant_id, id)`.
- **Indexes created on the parent** are created on every partition (each partition has its own local index). There's no global index.
- **Foreign keys** referencing a partitioned table are supported since PG 12, but they must reference the full unique key.
- **Updating the partition key** moves the row between partitions (delete + insert under the hood).
- **Planning cost** grows with partition count. Hundreds of partitions are fine; many thousands make planning and locking expensive (each touched partition takes a lock; see `max_locks_per_transaction`).

## In practice

### Time-partitioned events

```sql
CREATE TABLE events (
  id          bigint GENERATED ALWAYS AS IDENTITY,
  tenant_id   bigint NOT NULL,
  event_type  text NOT NULL,
  properties  jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (created_at, id)
) PARTITION BY RANGE (created_at);

CREATE TABLE events_2026_11 PARTITION OF events
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE events_2026_12 PARTITION OF events
  FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE TABLE events_default PARTITION OF events DEFAULT;

CREATE INDEX ON events (tenant_id, created_at);
```

Ranges are inclusive at `FROM`, exclusive at `TO`. Pick a granularity so partitions are roughly 10–100 GB or so and you have tens to low hundreds of them; monthly or weekly is common.

### Create partitions ahead of time

Inserts for a range with no partition fail (or land in the default partition). Create future partitions on a schedule, a few periods ahead, with `pg_partman` or a scheduled job:

```sql
SELECT partman.create_parent(
  p_parent_table => 'public.events',
  p_control      => 'created_at',
  p_interval     => '1 month',
  p_premake      => 3
);
```

> **Gotcha:** Keep the `DEFAULT` partition empty. Creating a new partition requires scanning the default partition to check that no existing rows belong in the new range, while holding a lock. A large default partition turns routine partition creation into a slow, blocking operation.

### Retention: drop, don't delete

Deleting 200M old rows generates WAL, bloat and vacuum work for hours. Dropping a partition is a metadata change:

```sql
ALTER TABLE events DETACH PARTITION events_2025_10 CONCURRENTLY;
DROP TABLE events_2025_10;   -- or archive it to cold storage first
```

`DETACH ... CONCURRENTLY` (PG 14+) avoids holding an `ACCESS EXCLUSIVE` lock on the parent; it can't run inside a transaction block or with a default partition present.

### Adding indexes on large partitioned tables

`CREATE INDEX CONCURRENTLY` isn't supported on the partitioned parent directly. Instead:

```sql
CREATE INDEX events_props_gin ON ONLY events USING gin (properties jsonb_path_ops);
-- invalid until every partition has one attached

CREATE INDEX CONCURRENTLY events_2026_11_props_gin
  ON events_2026_11 USING gin (properties jsonb_path_ops);
ALTER INDEX events_props_gin ATTACH PARTITION events_2026_11_props_gin;
-- repeat per partition; the parent index becomes valid when all are attached
```

### Attaching a pre-loaded partition

Load data into a standalone table, add a matching `CHECK` constraint so Postgres can skip the validation scan, then attach:

```sql
ALTER TABLE events_2027_01 ADD CONSTRAINT chk_range
  CHECK (created_at >= '2027-01-01' AND created_at < '2027-02-01');
ALTER TABLE events ATTACH PARTITION events_2027_01
  FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');
ALTER TABLE events_2027_01 DROP CONSTRAINT chk_range;
```

`ATTACH PARTITION` takes `SHARE UPDATE EXCLUSIVE` on the parent, so it doesn't block queries.

### Hash partitioning by tenant

```sql
CREATE TABLE document_chunks (...) PARTITION BY HASH (tenant_id);
CREATE TABLE document_chunks_p0 PARTITION OF document_chunks
  FOR VALUES WITH (MODULUS 16, REMAINDER 0);
-- ... p1 to p15
```

Useful when per-tenant queries dominate and you want smaller per-partition indexes (for example HNSW indexes for pgvector). Changing the modulus later means rewriting data, so choose generously.

## When partitioning doesn't help

- **Queries that don't filter on the key.** A per-tenant query on a time-partitioned table without a time filter scans every partition's index: N index probes instead of one.
- **Small tables.** Under ~50–100 GB, a good index usually wins.
- **Random-access OLTP by primary key** without the partition key: the planner can't prune.
- **Too many partitions.** Daily partitions for 5 years is 1,800 partitions; planning time and lock overhead show up on every query.

> **Rule of thumb:** Partition when you have a clear retention or lifecycle boundary and most queries filter on it. Time ranges on append-only data are the canonical case.

## Pitfalls

- **Unique constraints you can't express** because they don't include the partition key. Global uniqueness of `id` becomes the sequence's job, not a constraint's.
- **Forgetting future partitions.** Inserts fail at midnight on the 1st. Alert if fewer than N future partitions exist.
- **Fat default partition.** See gotcha above.
- **ORM assumptions.** Django doesn't manage partitions; create them with `RunSQL` migrations or `django-postgres-extra`. Composite primary keys need Django 5.2's `CompositePrimaryKey` or a workaround that keeps `id` as the ORM's pk.
- **Statistics on the parent.** Autovacuum analyzes partitions, not the parent. Run `ANALYZE events` periodically so queries spanning partitions get decent estimates.

## Checklist

- Choose the key from your retention policy and dominant `WHERE` clause.
- Include the key in every primary key and unique constraint.
- Automate creating partitions ahead of time; keep the default partition empty.
- Retain by `DETACH ... CONCURRENTLY` + `DROP`, never bulk `DELETE`.
- Verify pruning with `EXPLAIN` for your top queries.
- Keep partition count in the tens to low hundreds.

## Interview angle

**Q: When should you partition a table?**
When the table is large, has a natural lifecycle key (usually time), and most queries filter on that key. Main wins are cheap retention, bounded vacuum/index maintenance, and pruning.

**Q: What are the downsides?**
Unique constraints must include the key, no global indexes, more planning overhead, queries without the key touch every partition, and partition management becomes an operational task.

**Q: How do you delete old data from a 2 TB events table?**
Partition by time and drop or detach old partitions, which is instant and creates no bloat, instead of batch-deleting rows.
