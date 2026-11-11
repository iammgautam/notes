---
title: Reading EXPLAIN ANALYZE
section: postgresql
order: 2
summary: How to read a real execution plan, compare estimates to actuals, use BUFFERS, and find the node that actually costs time.
---

`EXPLAIN ANALYZE` is the single most useful tool for Postgres performance work. It runs the query and annotates every plan node with what really happened: rows, loops, time and buffer IO. Reading it fluently is the difference between guessing at indexes and fixing the right thing in one try.

## How it works

`EXPLAIN` alone shows the plan and **estimates**. `EXPLAIN ANALYZE` executes the query and adds **actuals**. Options worth knowing:

| Option | What it adds |
|---|---|
| `ANALYZE` | Runs the query, shows actual time, rows, loops. |
| `BUFFERS` | Shared/local/temp blocks hit, read, dirtied, written. Default on with `ANALYZE` since PG 18; pass it explicitly on 16/17. |
| `VERBOSE` | Output column lists, schema-qualified names. |
| `SETTINGS` | Non-default planner settings in effect. |
| `WAL` | WAL records and bytes generated (for writes). |
| `SERIALIZE` | (PG 17) Cost of converting rows to wire format, useful for wide rows or big JSONB. |
| `FORMAT JSON` | Machine-readable, for tools like explain.dalibo.com. |

> **Gotcha:** `EXPLAIN ANALYZE` really executes the statement. For `UPDATE`/`DELETE`/`INSERT`, wrap it in `BEGIN; ... ROLLBACK;`.

### Anatomy of a node

```text
Index Scan using events_tenant_created_idx on events
  (cost=0.56..812.40 rows=210 width=96)
  (actual time=0.031..4.912 rows=18342 loops=1)
  Index Cond: ((tenant_id = 42) AND (created_at >= '2026-10-01'))
  Buffers: shared hit=1203 read=4410
```

- `cost=startup..total`: estimated cost to first row and to all rows.
- `rows=210`: estimated rows **per loop**.
- `actual time=first..last` in milliseconds, **per loop**.
- `rows=18342`: actual rows **per loop**.
- `loops=1`: how many times this node ran. Multiply time and rows by loops for totals.
- `shared hit`: 8 KB pages found in `shared_buffers`. `read`: pages requested from the OS (page cache or disk).

Here the estimate is off by 87x. That is the thing to chase.

### Time is inclusive

Each node's time includes its children. To find where time goes, subtract children from parents. The node with the largest **exclusive** time is the hot spot.

## What to look for

1. **Estimate vs actual rows.** A 10x+ mismatch is the usual root cause of a bad join choice. Find the lowest node in the tree where it starts.
2. **Loops.** A `Nested Loop` whose inner side runs `loops=50000` at 0.1 ms each is 5 seconds.
3. **`Rows Removed by Filter`.** A large number means the index got you close but a filter is throwing away most rows. A better or composite index can push that predicate into `Index Cond`.
4. **`Heap Fetches`** on an `Index Only Scan`. High values mean the visibility map is stale; vacuum the table.
5. **Sorts and hashes spilling.** `Sort Method: external merge Disk: 512000kB` or `Batches: 16` on a hash means `work_mem` was too small.
6. **Buffers `read` vs `hit`.** Cold-cache runs look much slower than warm ones. Run twice and compare, and judge by buffers, which are stable, not just time.
7. **`Planning Time`.** Usually under 1 ms. If it is 50 ms, look at partition count, huge `IN` lists, or many joins.

## Worked example

Dashboard query: daily event counts for one tenant over 30 days.

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT date_trunc('day', created_at) AS day, count(*)
FROM events
WHERE tenant_id = 42
  AND event_type = 'page_view'
  AND created_at >= now() - interval '30 days'
GROUP BY 1
ORDER BY 1;
```

```text
Sort  (actual time=9120.4..9120.5 rows=30 loops=1)
  Sort Key: (date_trunc('day', created_at))
  ->  HashAggregate  (actual time=9120.1..9120.3 rows=30 loops=1)
        Group Key: date_trunc('day', created_at)
        ->  Bitmap Heap Scan on events
              (cost=1520.3..98012.4 rows=4120 width=8)
              (actual time=210.4..8890.7 rows=1204331 loops=1)
              Recheck Cond: (tenant_id = 42)
              Filter: ((event_type = 'page_view')
                       AND (created_at >= (now() - '30 days')))
              Rows Removed by Filter: 6210554
              Heap Blocks: exact=412003
              Buffers: shared hit=22010 read=398211
              ->  Bitmap Index Scan on events_tenant_id_idx
                    (actual time=180.2..180.2 rows=7414885 loops=1)
                    Index Cond: (tenant_id = 42)
Planning Time: 0.4 ms
Execution Time: 9121.8 ms
```

Reading it bottom up:

- The only usable index is on `tenant_id`, which matches 7.4M rows for this large tenant.
- 6.2M rows are removed by the filter. Most of the work is wasted.
- 398k pages read from outside shared buffers, about 3 GB of IO.
- The estimate (4,120 rows) is 300x below actual (1.2M), because the planner treats the three predicates as independent.

Fix: a composite index matching equality columns first, range last.

```sql
CREATE INDEX CONCURRENTLY events_tenant_type_created_idx
  ON events (tenant_id, event_type, created_at);
```

The new plan uses an `Index Only Scan` (if the visibility map is current) with all three predicates in `Index Cond`, no filter, and reads only the slice it needs. For a dashboard that runs constantly, the real fix is a rollup table so you read 30 rows, not 1.2M. See [Rollups and materialized views]({{ '/postgresql/rollups-and-materialized-views/' | relative_url }}).

## In practice

### Capture plans from production automatically

`auto_explain` logs plans for slow queries without anyone running `EXPLAIN` manually:

```text
shared_preload_libraries = 'auto_explain,pg_stat_statements'
auto_explain.log_min_duration = '500ms'
auto_explain.log_analyze = on
auto_explain.log_buffers = on
auto_explain.log_timing = off      # per-node timing is expensive
auto_explain.sample_rate = 0.1
```

### Find which queries to explain

```sql
SELECT queryid, calls, round(total_exec_time) AS total_ms,
       round(mean_exec_time, 1) AS mean_ms, rows,
       shared_blks_read, left(query, 80)
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Optimize by **total** time first. A 20 ms query called 2M times a day matters more than a 9 s report run twice.

### From Django

```python
qs = Event.objects.filter(tenant_id=42, event_type="page_view")
print(qs.explain(analyze=True, buffers=True))
```

## Pitfalls

- **Testing on a tiny dev database.** Plans on 10k rows tell you nothing about 100M rows. Test on production-shaped data or at least compare estimates.
- **Timing overhead.** `ANALYZE` instruments every row; on queries with millions of cheap rows, the measured time can be 2x real. Use `TIMING OFF` when you only need row counts.
- **Warm cache bias.** The second run is always faster. Compare `shared read` counts, not milliseconds.
- **Parameterized queries.** Pasting literal values may produce a different plan than the generic plan the app actually uses. Use `PREPARE` and `EXPLAIN EXECUTE` with `plan_cache_mode` matching production.
- **Ignoring `loops`.** Actual rows and time are per loop. A node showing `rows=1` and `loops=200000` produced 200k rows.

> **Rule of thumb:** Read the plan bottom up, find the first node where estimated and actual rows diverge by 10x or more, and fix that estimate or that access path. Everything above it is a consequence.

## Checklist

- Always use `EXPLAIN (ANALYZE, BUFFERS)`; add `SETTINGS` when comparing environments.
- Wrap writes in a rolled-back transaction.
- Look for: estimate mismatch, high loops, `Rows Removed by Filter`, disk sorts, `Heap Fetches`.
- Use `pg_stat_statements` to choose targets and `auto_explain` to catch them in production.
- Paste large plans into a visualizer (explain.dalibo.com, explain.depesz.com) to see exclusive time.

## Interview angle

**Q: The query is slow; what is your first step?**
Find it in `pg_stat_statements`, run `EXPLAIN (ANALYZE, BUFFERS)` on production-like data, then locate the node with the largest exclusive time and the first big estimate/actual mismatch.

**Q: What does `Rows Removed by Filter` tell you?**
The access path returned rows that a later predicate discarded. The index is not selective enough for the full predicate; a composite or partial index can move the filter into the index condition.

**Q: Why trust buffers more than time?**
Time depends on cache state and machine load. Buffer counts measure the work done and are reproducible.
