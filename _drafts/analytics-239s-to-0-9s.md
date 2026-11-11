---
title: "From 239 seconds to 0.9: fixing a Postgres analytics query"
summary: "How precomputed rollups, partial and composite indexes, JSONB and full-text search took a measured analytics query from 239s to 0.9s."
tags: [postgresql, performance]
---

One of the things I worked on at Gainfront was PostgreSQL analytics performance. The headline number: a measured query time that went from **239 seconds to 0.9 seconds**, on the same PostgreSQL. The work was precomputed rollups, an indexing pass that ended up at 56+ indexes (partial and composite among them), and making proper use of JSONB and full-text search.

This post is about how to approach that kind of problem, in roughly the order I would do it again.

## The problem

Analytics queries in a multi-tenant SaaS have a nasty shape. They aggregate over large, ever-growing tables. They filter on a mix of fixed columns, semi-structured attributes and free text. And every tenant sees them on a dashboard, where "a few seconds" already feels broken and four minutes is simply a timeout.

> **TODO (author):** describe the screen or report this query powered, in generic terms (what the user was trying to see), and how the slowness showed up (timeouts, user complaints, Sentry, slow query log).

A 239-second query is not a "tune one setting" problem. It means the database is doing far more work than the answer needs, usually by scanning and aggregating raw rows on every request.

## Constraints

- **Correctness first.** Analytics numbers end up in decisions. A faster wrong number is worse than a slow right one.
- **Stay on Postgres.** The data, the tenancy model and the application already lived there. A separate warehouse would have been a much bigger project.
- **Writes still matter.** Every index and every rollup adds write cost. The transactional side of the app could not get noticeably slower to pay for reads.

## Measure before touching anything

The single most useful habit here is refusing to guess. Start and end every change with `EXPLAIN (ANALYZE, BUFFERS)` on realistic data, ideally a large tenant's.

```sql
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT ...;
```

What I look for in the plan, in order:

1. **Where the time actually goes.** Read the `actual time` on each node, not the estimated cost. The slow node is often not the one you expected.
2. **Estimated vs actual rows.** If the planner expects 10 rows and gets 2 million, every decision above that node is wrong. Stale statistics or correlated columns are the usual suspects; `ANALYZE` and extended statistics are the fixes.
3. **`Seq Scan` on a big table with a selective filter.** A missing or unusable index.
4. **`Rows Removed by Filter`** in the hundreds of thousands. The index found rows, then the database threw most of them away.
5. **`Buffers: shared read`** versus `shared hit`. Lots of reads means you are paying for disk; compare a cold run with a warm one before celebrating.
6. **Sorts and hashes spilling to disk** (`external merge`, `Batches: > 1`).

> **Rule of thumb:** run each version several times and compare warm-cache numbers to warm-cache numbers. A 10x "win" that is really just a warmed buffer cache will disappear in production.

`pg_stat_statements` is the other half: it tells you which queries cost the most in aggregate, which is not always the slowest single query.

> **TODO (author):** paste the before/after `EXPLAIN (ANALYZE, BUFFERS)` output (trimmed, with table and column names anonymized).

## What I tried, in order of impact

I cannot honestly split the 239s → 0.9s into "this much from rollups, this much from indexes"; it was the combination. But the order of attack matters, so here it is.

> **TODO (author):** if you do know the rough contribution of each step, add it here. Otherwise leave this paragraph as is.

### 1. Precomputed rollups

The biggest structural change is to stop aggregating raw events on every request. If a dashboard shows counts and sums per tenant per day per dimension, store exactly that.

A simplified version of the pattern:

```sql
CREATE TABLE daily_rollup (
    tenant_id  bigint  NOT NULL,
    day        date    NOT NULL,
    dimension  text    NOT NULL,
    item_count bigint  NOT NULL,
    total      numeric NOT NULL,
    PRIMARY KEY (tenant_id, day, dimension)
);

-- Refresh a window of days for one tenant. Idempotent: safe to re-run.
INSERT INTO daily_rollup (tenant_id, day, dimension, item_count, total)
SELECT tenant_id,
       created_at::date,
       dimension,
       count(*),
       sum(amount)
FROM   fact_table
WHERE  tenant_id = %(tenant)s
  AND  created_at >= %(from)s
  AND  created_at <  %(to)s
GROUP  BY 1, 2, 3
ON CONFLICT (tenant_id, day, dimension)
DO UPDATE SET item_count = EXCLUDED.item_count,
              total      = EXCLUDED.total;
```

Now the dashboard query reads a few hundred rollup rows instead of aggregating millions of fact rows. The design questions are about freshness, not SQL:

- How stale can the number be? Seconds, minutes, a day?
- What triggers a refresh: a schedule, a write, or both?
- How do late-arriving or corrected rows get folded back in? Re-running a window with `ON CONFLICT DO UPDATE` keeps it idempotent.

> **TODO (author):** describe how the rollups were refreshed in practice (schedule, trigger, background job) and the freshness the product accepted.

Materialized views are the built-in alternative. They are great when a full refresh is cheap enough; `REFRESH MATERIALIZED VIEW CONCURRENTLY` avoids blocking readers but still recomputes everything. An explicit rollup table lets you refresh just the window that changed.

### 2. Composite indexes that match the query

In a multi-tenant schema almost every query starts with `tenant_id = ?`. A composite index that leads with the tenant and continues with the range or sort column turns a big scan into a narrow range scan:

```sql
CREATE INDEX CONCURRENTLY fact_tenant_created_idx
    ON fact_table (tenant_id, created_at);
```

Column order is the whole game: equality columns first, then the range or `ORDER BY` column. An index on `(created_at, tenant_id)` looks similar and behaves very differently.

When the query only needs a few columns, `INCLUDE` can make it an index-only scan:

```sql
CREATE INDEX CONCURRENTLY fact_tenant_created_cov_idx
    ON fact_table (tenant_id, created_at) INCLUDE (amount);
```

### 3. Partial indexes for the rows you actually query

Analytics often ignores most of the table: soft-deleted rows, archived records, a status nobody reports on. A partial index only covers what matters, so it is smaller, stays in memory more easily and is cheaper to maintain:

```sql
CREATE INDEX CONCURRENTLY fact_active_idx
    ON fact_table (tenant_id, created_at)
    WHERE deleted_at IS NULL;
```

> **Gotcha:** the query's `WHERE` clause must imply the index predicate, in a form the planner can prove. If the ORM writes `deleted_at IS NULL` and the index says the same, you are fine. A parameterized `status = %s` will not match a partial index on `status = 'active'`.

### 4. JSONB done properly

Semi-structured attributes in JSONB are flexible, and slow if you filter on them without help. Two tools:

```sql
-- Containment queries: attrs @> '{"region": "south"}'
CREATE INDEX CONCURRENTLY fact_attrs_gin
    ON fact_table USING gin (attrs jsonb_path_ops);

-- One hot key used in equality filters and GROUP BY
CREATE INDEX CONCURRENTLY fact_attrs_region_idx
    ON fact_table (tenant_id, (attrs->>'region'));
```

`jsonb_path_ops` is smaller and faster than the default GIN operator class, but only supports containment (`@>`). And a GIN index will not help `attrs->>'region' = 'south'`; that needs the expression index, and the query must use exactly the same expression.

If one key is queried constantly, promoting it to a real column (or a generated column) is often cleaner than any JSONB index.

### 5. Full-text search instead of ILIKE

`ILIKE '%term%'` on a large text column cannot use a B-tree index and scans everything. A stored `tsvector` with a GIN index fixes that:

```sql
ALTER TABLE fact_table
    ADD COLUMN search tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(notes, ''))
    ) STORED;

CREATE INDEX CONCURRENTLY fact_search_idx
    ON fact_table USING gin (search);

-- Query
SELECT ... FROM fact_table
WHERE tenant_id = %(tenant)s
  AND search @@ plainto_tsquery('simple', %(q)s);
```

The text search configuration (`simple`, `english`, ...) changes stemming and stop words, so pick it on purpose and use the same one in the column and the query.

## Results

The measured query time went from **239s to 0.9s**. Along the way the schema gained 56+ indexes, including partial and composite ones, plus precomputed rollups and JSONB and full-text search indexes.

> **TODO (author):** state what exactly was measured (which query or endpoint, which environment, cold or warm cache) so readers can trust the comparison.

## What I would do differently

- **Audit indexes as often as you add them.** Fifty-plus indexes is a lot of write amplification. `pg_stat_user_indexes.idx_scan` tells you which ones are never used; those should go. I would put that check on a schedule from the start.
- **Write the freshness contract down before building rollups.** "How stale is acceptable?" is a product question. Answering it late means rebuilding the refresh logic.
- **Keep a benchmark query set.** A handful of representative queries with their plans checked into the repo makes regressions obvious when someone changes a model.
- **Always `CREATE INDEX CONCURRENTLY` in production.** A plain `CREATE INDEX` blocks writes for the whole build. In Django that means a migration with `atomic = False` and `AddIndexConcurrently` from `django.contrib.postgres.operations`.

The broader lesson: the fix for a slow analytics query is rarely one clever index. It is reducing the amount of data the query has to touch (rollups), then making sure the remaining access paths are exactly the ones the query needs (indexes that match its shape). `EXPLAIN ANALYZE` tells you which of the two you are missing.
