---
title: How Postgres executes a query
section: postgresql
order: 1
summary: The path from SQL text to rows, how the planner uses statistics to pick scans and joins, and why bad estimates cause slow queries.
---

Every slow query you will ever debug comes down to one question: why did the planner choose this plan? To answer it you need a working model of the pipeline a query goes through and of the statistics the planner trusts. This note gives you that model.

## How it works

A query passes through four stages inside the backend process that owns your connection.

```text
SQL text
   |
   v
[parse]    -> raw parse tree (syntax only)
   |
[analyze]  -> query tree (names resolved to OIDs, types checked)
   |
[rewrite]  -> views expanded, rules applied, RLS policies added
   |
[plan]     -> cheapest plan tree by estimated cost
   |
[execute]  -> volcano-style iterator pulls rows up the tree
```

### Parse and analyze

The parser checks grammar only. The analyzer looks up tables, columns and functions in the catalog and resolves types. Errors like `column "tenantid" does not exist` come from here. Nothing about data has been read yet.

### Rewrite

The rewriter replaces view references with their definitions and applies rules. Row-level security policies are also injected around this point, so an RLS predicate like `tenant_id = current_setting('app.tenant_id')::bigint` becomes part of the query before planning. That matters: the planner sees the policy predicate and can use an index on `tenant_id`.

### Plan

The planner enumerates candidate plans and picks the cheapest by **estimated cost**, measured in abstract units anchored to `seq_page_cost = 1.0`. Other knobs:

| Setting | Default | Meaning |
|---|---|---|
| `random_page_cost` | 4.0 | Cost of a random page read. Set to ~1.1 on SSDs. |
| `cpu_tuple_cost` | 0.01 | Processing one row. |
| `cpu_index_tuple_cost` | 0.005 | Processing one index entry. |
| `cpu_operator_cost` | 0.0025 | Evaluating one operator or function. |
| `effective_cache_size` | 4GB | Hint about OS + shared cache size; makes index scans look cheaper. |

Cost depends on **row count estimates**, and those come from statistics gathered by `ANALYZE` (and autovacuum's auto-analyze) into `pg_statistic`, readable through `pg_stats`:

- `null_frac`, `n_distinct`
- `most_common_vals` / `most_common_freqs` (MCV list)
- `histogram_bounds` for values outside the MCV list
- `correlation`: how well physical row order matches column order (drives index scan cost)

The sample size is `300 * default_statistics_target` rows (default target 100, so 30,000 rows). For each predicate the planner computes a **selectivity**, multiplies it by `reltuples`, and gets an estimated row count.

For joins of more than `geqo_threshold` (12) relations, Postgres switches to a genetic optimizer instead of exhaustive search. `join_collapse_limit` and `from_collapse_limit` (8) bound how much join reordering is attempted.

### Execute

The plan is a tree of nodes. The executor calls the root node, which pulls rows from its children one at a time. This is why `LIMIT` can stop a scan early, and why a `Sort` or `HashAggregate` must consume all its input before emitting anything.

## Scan types

| Node | When the planner picks it |
|---|---|
| `Seq Scan` | Large fraction of table needed, or no usable index. |
| `Index Scan` | Few rows; fetches each heap tuple by TID in index order. |
| `Index Only Scan` | All needed columns are in the index and pages are all-visible. |
| `Bitmap Index Scan` + `Bitmap Heap Scan` | Medium selectivity, or combining several indexes with AND/OR. Reads heap pages in physical order. |
| `Tid Scan` | `WHERE ctid = ...` (rare in app code). |

The crossover from index to seq scan is often around 5–10% of the table, but it shifts with `random_page_cost`, correlation and caching.

## Join types

| Node | Good for | Cost shape |
|---|---|---|
| `Nested Loop` | Small outer side, indexed inner lookup | `O(outer * inner_lookup)` |
| `Hash Join` | Equality joins, inner side fits in `work_mem` | `O(n + m)`, builds hash on smaller side |
| `Merge Join` | Both sides already sorted (or cheap to sort) on join key | `O(n + m)` after sorting |

Nested loops are fantastic when the outer side really has 10 rows and catastrophic when the planner thought 10 and got 500,000. Most "the query was fine yesterday" incidents are exactly this.

## In practice

Inspect what the planner believes about a column:

```sql
SELECT attname, null_frac, n_distinct, most_common_vals, correlation
FROM pg_stats
WHERE tablename = 'events' AND attname IN ('tenant_id', 'event_type');
```

Inspect table-level estimates:

```sql
SELECT relname, reltuples::bigint, relpages
FROM pg_class
WHERE relname = 'events';
```

Raise statistics detail for a skewed column (one tenant owns 40% of rows):

```sql
ALTER TABLE events ALTER COLUMN tenant_id SET STATISTICS 1000;
ANALYZE events;
```

Teach the planner about correlated columns. Without this, `WHERE country = 'IN' AND city = 'Bengaluru'` multiplies two selectivities as if they were independent and underestimates badly:

```sql
CREATE STATISTICS addr_stats (dependencies, ndistinct, mcv)
  ON country, city FROM customers;
ANALYZE customers;
```

On SSD storage, tell the planner random IO is cheap:

```sql
ALTER SYSTEM SET random_page_cost = 1.1;
SELECT pg_reload_conf();
```

### Prepared statements and generic plans

With server-side prepared statements, Postgres plans the first five executions with the actual parameter values (custom plans), then compares them to a generic plan. If the generic plan is not much worse on average, it sticks with it. For skewed data (a huge tenant vs. a tiny one) the generic plan can be terrible for some parameter values.

```sql
SET plan_cache_mode = force_custom_plan;  -- per session or per role
```

Django uses client-side parameter binding with psycopg2, so this mostly matters with psycopg 3 server-side binding, PgBouncer-prepared statements, or PL/pgSQL functions (which cache plans).

## Pitfalls

- **Stale statistics after bulk loads.** You load 10M rows into a staging table and immediately query it; `reltuples` still says 0 and the planner picks nested loops. Run `ANALYZE` explicitly after bulk loads and in migrations that backfill data.
- **Functions hide selectivity.** `WHERE lower(email) = $1` has no statistics unless an expression index exists on `lower(email)` (expression indexes collect their own stats). A default selectivity of 0.5% or so is guessed.
- **Correlated predicates.** `tenant_id = 7 AND project_id = 123` where project implies tenant. The planner multiplies selectivities and underestimates. Extended statistics fix it.
- **CTEs.** Since PG 12, non-recursive CTEs referenced once are inlined. `WITH ... AS MATERIALIZED` forces the old optimization-fence behavior; use it deliberately.
- **`OFFSET` pagination.** The executor must produce and discard every skipped row. Page 5,000 is slow no matter what index you have. Use keyset pagination.
- **JIT on OLTP.** `jit = on` with default thresholds can add 50–200 ms of compilation to a query that runs in 5 ms when the cost estimate is inflated. Many OLTP deployments set `jit = off` or raise `jit_above_cost`.

> **Gotcha:** Bad plans are usually caused by bad row estimates, not bad cost constants. Fix the estimate (statistics, extended stats, query rewrite) before touching `random_page_cost` or `enable_*` flags.

## Rules of thumb

- When a plan is wrong, compare estimated vs actual rows node by node; the first big mismatch is the root cause.
- Run `ANALYZE` after any bulk write in a migration or ETL job.
- Use `CREATE STATISTICS` for columns that are logically dependent.
- Set `random_page_cost` to 1.1–1.5 on SSD/NVMe.
- Never ship `SET enable_seqscan = off` to production; use it only to test whether a plan is possible.
- Prefer keyset pagination over `OFFSET` for anything user-facing.

## Interview angle

**Q: What happens when you send a query to Postgres?**
Parse, analyze (resolve names), rewrite (views, RLS), plan (cost-based search using statistics), execute (iterator tree pulls rows). Each connection has its own backend process doing this.

**Q: Why might Postgres ignore an index?**
The planner estimated that enough rows match that a seq scan is cheaper, the predicate does not match the index (function on the column, type mismatch, leading column missing), or statistics are stale.

**Q: Nested loop vs hash join?**
Nested loop wins when the outer side is small and the inner side has an index. Hash join wins for large equality joins where one side fits in `work_mem`. A nested loop chosen on a bad estimate is a classic performance cliff.
