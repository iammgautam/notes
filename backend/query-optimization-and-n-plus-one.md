---
title: Query optimization and the N+1 problem
section: backend
order: 56
phase: "Phase 8 · Performance and reliability"
summary: Find expensive queries with pg_stat_statements, fix them with EXPLAIN, indexes and rewrites, and remove N+1 patterns in Django and GraphQL.
---

Most backend latency problems end at the database: one query that scans two million rows to return fifty, or fifty small queries issued one at a time by an ORM loop. Both yield to a repeatable process: rank statements by aggregate cost, read their plans, change one thing, verify. This note covers that loop for PostgreSQL and the most common ORM-shaped problem, N+1 queries.

## Database query optimization

### The optimization loop

```text
 +----------------------+      +---------------------------+
 | pg_stat_statements   | ---> | EXPLAIN (ANALYZE, BUFFERS)|
 | rank by total time   |      | with the slow parameters  |
 +----------------------+      +---------------------------+
            ^                                |
            |                                v
 +----------------------+      +---------------------------+
 | verify: plan, p95,   | <--- | one change: index, query  |
 | stats after deploy   |      | rewrite, statistics, conf |
 +----------------------+      +---------------------------+
```

Rank by **total** time (`calls × mean`), not by the slowest statement: a 2 ms query called 40 million times a day costs more than a 4 s report that runs twice. Change one thing at a time.

### Finding the queries with pg_stat_statements

`pg_stat_statements` aggregates every normalized statement (constants replaced by `$1`, `$2`) with calls, timing, rows and buffer usage. It needs `shared_preload_libraries = 'pg_stat_statements'`, a restart, and `CREATE EXTENSION pg_stat_statements;`.

```sql
SELECT queryid,
       calls,
       round(total_exec_time::numeric)    AS total_ms,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       rows / nullif(calls, 0)            AS rows_per_call,
       shared_blks_read                   AS blocks_from_disk,
       left(query, 80)                    AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

How to read it:

- Huge `calls`, sub-millisecond `mean_ms`, one row per call, `WHERE id = $1`: an N+1 loop.
- High `blocks_from_disk`: the working set doesn't fit in memory.
- `rows_per_call` far above what the endpoint shows: filtering happens in Python.
- `pg_stat_statements_reset()` before and after a fix gives a clean comparison.

For "it was slow at 3 a.m." problems, `auto_explain` logs plans of statements over a threshold (`auto_explain.log_min_duration = '500ms'`). Its `log_analyze` option instruments every statement, so pair it with `auto_explain.sample_rate` on busy systems.

### Reading the plan

Run `EXPLAIN (ANALYZE, BUFFERS)` with the parameter values that are actually slow (the tenant with 40% of the rows, not tenant 1). Look for:

- The node where actual time jumps. Times are cumulative and per loop, so multiply by `loops`.
- Estimated vs actual `rows` off by 10× or more: bad statistics. Run `ANALYZE`, raise the statistics target, or add `CREATE STATISTICS` for correlated columns.
- `Rows Removed by Filter` in the hundreds of thousands to return a handful: missing or wrong index.
- `Sort Method: external merge  Disk: ...`: the sort spilled. Raise `work_mem` for that query or let an index provide the order.
- A Nested Loop with `loops=50000` on its inner side: usually a misestimate.

The full walkthrough is in [Reading EXPLAIN ANALYZE]({{ '/postgresql/explain-analyze/' | relative_url }}).

### Choosing an index

| Query shape | Index | Why |
|---|---|---|
| `tenant_id = ? AND status = ? ORDER BY created_at DESC LIMIT 50` | `(tenant_id, status, created_at DESC)` | Equality columns first, sort column last: the scan stops after 50 rows, no sort |
| `tenant_id = ? AND created_at >= ?` | `(tenant_id, created_at)` | Equality before range; columns after a range are not used for seeking |
| Hot query reads three columns | add `INCLUDE (total, currency)` | Index-only scan, no heap visits (if the visibility map is current) |
| `status = 'pending'` matches 1% of rows | partial index `WHERE status = 'pending'` | Small, cheap to maintain, stays in memory |
| `lower(email) = ?` | expression index on `lower(email)` | The index must match the expression exactly |
| JSONB containment `attrs @> ?` | GIN with `jsonb_path_ops` | Many keys, one index |
| Append-only events scanned by time range | BRIN on `created_at` | Kilobytes instead of gigabytes; needs physical correlation |

Every index costs write amplification, WAL, vacuum work and RAM, and indexing a frequently updated column disables HOT updates. Drop indexes whose `pg_stat_user_indexes.idx_scan` stays at zero. Column-order rules in depth: [Indexing strategies]({{ '/backend/indexing-strategies/' | relative_url }}) and [B-tree indexes]({{ '/postgresql/btree-indexes/' | relative_url }}).

### Rewriting queries

| Anti-pattern | Rewrite |
|---|---|
| Function or cast on the column: `date(created_at) = '2026-11-01'` | Range on the raw column: `created_at >= '2026-11-01' AND created_at < '2026-11-02'` |
| `NOT IN (SELECT ...)` | `NOT EXISTS (...)`: correct with NULLs, plans as an anti-join |
| Correlated subquery in the `SELECT` list per row | `JOIN` + `GROUP BY`, or `LATERAL` for top-N per group |
| `SELECT *` on rows with large JSONB or text | Select the columns you use; enables index-only scans and avoids TOAST reads |
| `OR` across different columns that defeats the plan | Let BitmapOr handle it, or split into `UNION ALL` of two indexed queries |
| Thousands of literals in `IN (...)` built in Python | One array parameter (`= ANY(%s)`), or join against `unnest` or a temp table |
| Fetch 100k rows to sum or filter in Python | Push the filter and aggregate into SQL |

### Pagination

`LIMIT 50 OFFSET 100000` reads and throws away 100,050 rows, so each page is slower than the last, and rows shift between pages under concurrent inserts. Keyset (seek) pagination remembers the last row and seeks past it:

```sql
-- index: (tenant_id, created_at DESC, id DESC)
SELECT id, created_at, total
FROM orders
WHERE tenant_id = $1
  AND (created_at, id) < ($2, $3)     -- last row of the previous page
ORDER BY created_at DESC, id DESC
LIMIT 50;
```

Cost is constant per page. The `id` tiebreaker makes the order total, which keeps rows from being skipped or repeated. The trade-off is no random "jump to page 400". Cursor encoding and API shape are in [Pagination]({{ '/backend/pagination/' | relative_url }}).

### Aggregates

Under MVCC, PostgreSQL keeps no cached row count: `count(*)` must check visibility of every candidate row, and an index-only scan only helps when the visibility map is current (see [MVCC and vacuum]({{ '/postgresql/mvcc-and-vacuum/' | relative_url }})). Options, cheapest first:

- Don't count: "show more" instead of "page 1 of 8,231".
- Estimate from `pg_class.reltuples` or the planner's row estimate.
- Several counters in one pass: `count(*) FILTER (WHERE status = 'open')`.
- Precompute: rollup tables maintained by a job or trigger, or a materialized view refreshed with `REFRESH MATERIALIZED VIEW CONCURRENTLY` (needs a unique index).
- Exact counts over a small, indexed slice (one tenant, one day) are fine.

## The N+1 query problem

N+1 is one query to load N parents, then one more query per parent for a related object. Each query is fast on the server, but each pays a network round trip (roughly 0.2–1 ms inside a data center) plus driver and ORM overhead. A 50-row page with a customer and line items per row becomes 101 queries; nest one more level and it becomes `N × M`.

```text
 N+1 (lazy loading)                    Batched
 ----------------------------------    -------------------------------
 SELECT ... FROM orders LIMIT 50       SELECT ... FROM orders LIMIT 50
 SELECT ... FROM customer WHERE id=1   SELECT ... FROM customer
 SELECT ... FROM customer WHERE id=2     WHERE id IN (1, 2, ..., 50)
 ...
 SELECT ... FROM customer WHERE id=50
 = 51 round trips, grows with N        = 2 round trips, constant
```

ORMs load relations lazily: `order.customer` on an instance whose relation isn't loaded silently runs a query. The triggering loop hides in serializers, templates, `__str__` and model properties, far from the view that built the queryset.

### Detecting it

- **Development:** django-debug-toolbar's SQL panel flags similar and duplicate queries; the `nplusone` package can raise on lazy loads.
- **Tests:** pin the query count with `assertNumQueries`, pytest-django's `django_assert_num_queries`, or `CaptureQueriesContext` comparing a 5-row and a 50-row page.
- **Production:** APM traces show a staircase of identical DB spans (Sentry, for example, flags this as an N+1 issue); `pg_stat_statements` shows a `WHERE id = $1` statement with enormous call counts.

### Fixes in the Django ORM

| Tool | Use for | SQL produced |
|---|---|---|
| `select_related("customer")` | Forward FK, one-to-one | One query with a JOIN (LEFT OUTER for nullable FKs) |
| `prefetch_related("items")` | Reverse FK, M2M, generic relations | A second query with `IN (...)`, stitched in Python |
| `Prefetch("items", queryset=..., to_attr=...)` | Filtered, ordered or slimmed children | Custom second query |
| `annotate(n=Count("items"))` | Per-row counts and sums | `GROUP BY` in the same query |
| `Subquery(...)` with `OuterRef` | "Latest X per row" | Correlated subquery, still one statement |
| `only()` / `defer()` | Wide rows | Fewer columns; touching a deferred field is a per-row query |
| `in_bulk(ids)` | Lookups by many ids | One `IN` query returning a dict |

To-many relations use a separate query because a JOIN would repeat each parent row once per child.

### DataLoader and batching in GraphQL

GraphQL makes N+1 the default: each field resolver runs independently, so `orders { customer { name } }` calls the customer resolver once per order. DataLoader, a pattern and library published by Facebook alongside GraphQL, fixes it with two ideas:

1. **Batching.** `load(key)` doesn't query. It queues the key and returns a future. When the current event-loop tick finishes, the loader calls one batch function with all queued keys, which must return values in the same order as the keys.
2. **Per-request memoization.** The same key in one request resolves once.

Create loaders per request, or one tenant's cached rows leak to another. Strawberry ships a `DataLoader`; graphene stacks commonly use `aiodataloader`.

### Batching beyond the database

Any loop doing I/O has the same shape: one Redis `GET` per item (use `MGET` or a pipeline), one HTTP call per id (add a batch endpoint), one `save()` per row (use `bulk_create`, `bulk_update` or `COPY`; in autocommit, 1,000 saves are also 1,000 commits).

## In practice

### A DRF list endpoint (illustrative)

A serializer that reads `customer.name`, nests `items`, and computes `obj.items.count()` in a `SerializerMethodField` costs `1 + 3N` queries per page if the viewset returns a bare `filter(tenant_id=...)`. The fix lives in `get_queryset`:

```python
from django.db.models import Count, Prefetch

class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name")
    items = OrderItemSerializer(many=True)
    item_count = serializers.IntegerField(read_only=True)   # from annotate


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSerializer

    def get_queryset(self):
        items = OrderItem.objects.only("id", "order", "sku", "qty", "price")
        return (
            Order.objects.filter(tenant_id=self.request.tenant.id)
            .select_related("customer")
            .prefetch_related(Prefetch("items", queryset=items))
            .annotate(item_count=Count("items"))
            .order_by("-created_at", "-id")
        )
```

Lock it in with a test that fails if the count grows with data:

```python
from django.db import connection
from django.test.utils import CaptureQueriesContext

def count_queries(client, url):
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 200
    return len(ctx.captured_queries)

def test_order_list_has_no_n_plus_one(api_client, make_orders):
    make_orders(5)
    baseline = count_queries(api_client, "/api/orders/")
    make_orders(45)
    assert count_queries(api_client, "/api/orders/") == baseline
```

### An index fix, verified with EXPLAIN

`QuerySet.explain(analyze=True, buffers=True)` prints the same plan from Django. In SQL (output trimmed, `COSTS OFF` for width):

```sql
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, total, created_at FROM orders
WHERE tenant_id = 42 AND status = 'open'
ORDER BY created_at DESC LIMIT 50;
```

```text
Limit (actual time=812.110..812.121 rows=50 loops=1)
  ->  Sort (actual time=812.108..812.114 rows=50 loops=1)
        Sort Key: created_at DESC
        Sort Method: top-N heapsort  Memory: 31kB
        ->  Seq Scan on orders
              (actual time=0.031..790.402 rows=51210 loops=1)
              Filter: ((tenant_id = 42) AND (status = 'open'::text))
              Rows Removed by Filter: 1948790
              Buffers: shared hit=1024 read=24547
Execution Time: 812.190 ms
```

Two million rows read for 50 returned, plus a sort. One index serves both the filter and the order:

```sql
CREATE INDEX CONCURRENTLY orders_tenant_status_created_idx
    ON orders (tenant_id, status, created_at DESC);
```

```text
Limit (actual time=0.041..0.212 rows=50 loops=1)
  ->  Index Scan using orders_tenant_status_created_idx on orders
        (actual time=0.040..0.205 rows=50 loops=1)
        Index Cond: ((tenant_id = 42) AND (status = 'open'::text))
        Buffers: shared hit=54
Execution Time: 0.240 ms
```

### N+1, prefetch and a DataLoader, measured

A runnable stdlib demo using SQLite. `set_trace_callback` counts statements the way `CaptureQueriesContext` does in Django.

```python
import asyncio
import sqlite3
from collections import defaultdict


def setup():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE author (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE book (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT);
        CREATE INDEX book_author_idx ON book (author_id);
    """)
    db.executemany("INSERT INTO author VALUES (?, ?)",
                   [(i, f"author-{i}") for i in range(1, 51)])
    db.executemany("INSERT INTO book (author_id, title) VALUES (?, ?)",
                   [(a, f"book-{a}-{n}") for a in range(1, 51) for n in range(3)])
    return db


class QueryCounter:
    def __init__(self, db):
        self.count = 0
        db.set_trace_callback(self._trace)

    def _trace(self, sql):
        if sql.lstrip().upper().startswith("SELECT"):
            self.count += 1


def n_plus_one(db):
    out = {}
    for author_id, name in db.execute("SELECT id, name FROM author ORDER BY id").fetchall():
        rows = db.execute("SELECT title FROM book WHERE author_id = ? ORDER BY id",
                          (author_id,)).fetchall()
        out[name] = [t for (t,) in rows]
    return out


def books_by_author(db, author_ids):
    """The batch query: one IN (...) for any number of parents."""
    marks = ",".join("?" * len(author_ids))
    grouped = defaultdict(list)
    sql = f"SELECT author_id, title FROM book WHERE author_id IN ({marks}) ORDER BY id"
    for author_id, title in db.execute(sql, author_ids):
        grouped[author_id].append(title)
    return grouped


def prefetch(db):
    authors = db.execute("SELECT id, name FROM author ORDER BY id").fetchall()
    grouped = books_by_author(db, [a for a, _ in authors])
    return {name: grouped[a] for a, name in authors}


class DataLoader:
    """Queue keys during one event-loop tick, then resolve them with one batch call."""

    def __init__(self, batch_fn):
        self.batch_fn = batch_fn
        self.cache = {}          # per-request memoization: key -> future
        self.queue = []

    def load(self, key):
        if key in self.cache:
            return self.cache[key]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.cache[key] = fut
        self.queue.append((key, fut))
        if len(self.queue) == 1:
            loop.call_soon(self._dispatch)   # runs after every resolver queued its key
        return fut

    def _dispatch(self):
        batch, self.queue = self.queue, []
        results = self.batch_fn([k for k, _ in batch])
        for key, fut in batch:
            fut.set_result(results.get(key, []))


async def graphql_style(db):
    authors = db.execute("SELECT id, name FROM author ORDER BY id").fetchall()
    loader = DataLoader(lambda ids: books_by_author(db, ids))

    async def resolve_books(author_id):      # one resolver call per author
        return await loader.load(author_id)

    books = await asyncio.gather(*(resolve_books(a) for a, _ in authors))
    return {name: titles for (_, name), titles in zip(authors, books)}


if __name__ == "__main__":
    db = setup()
    counter = QueryCounter(db)

    expected = None
    for label, fn, queries in [("n+1", n_plus_one, 51), ("prefetch", prefetch, 2)]:
        counter.count = 0
        result = fn(db)
        assert counter.count == queries, (label, counter.count)
        expected = expected or result
        assert result == expected
        print(f"{label:10} {counter.count:3} queries")

    counter.count = 0
    assert asyncio.run(graphql_style(db)) == expected
    assert counter.count == 2, counter.count
    print(f"{'dataloader':10} {counter.count:3} queries")
```

## Pitfalls

- **Benchmarking on a tiny dataset.** On a 1,000-row table the planner rightly prefers a sequential scan, so your index looks useless locally. Test with production-sized data.
- **Fast in psql, slow from the app.** With server-side prepared statements, PostgreSQL may switch to a generic plan after five executions, which can be terrible for skewed values (one giant tenant). Check `plan_cache_mode`; `force_custom_plan` for the affected role is a common fix.
- **Prefetch silently bypassed.** `order.items.filter(active=True)` inside a loop ignores the prefetched cache and queries per row. Prefetch the filtered set with `Prefetch("items", queryset=..., to_attr="active_items")`.
- **Over-prefetching.** Prefetching a relation with 10,000 children per parent loads it all into memory. Aggregate in SQL or paginate the children.
- **Index exists but isn't used.** Type mismatch, function on the column, low selectivity or stale statistics; `EXPLAIN` tells you which.
- **Building indexes with locks.** Plain `CREATE INDEX` blocks writes for the whole build; use `CONCURRENTLY` (outside a transaction, so a separate migration with `atomic = False` in Django). See [Zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).
- **Optimizing the wrong layer.** A page that needs 40 queries' worth of data on every request may want a cache or a read model, not an index. See [Caching patterns]({{ '/backend/caching-patterns/' | relative_url }}).

## Interview questions

**How do you find which queries to optimize first?**
Sort `pg_stat_statements` by `total_exec_time` (calls times mean), run `EXPLAIN (ANALYZE, BUFFERS)` on the top statements with realistic parameters, and fix the node that dominates. APM traces map each statement to its endpoint.

**What is the N+1 problem and how do you detect it?**
Loading N parents with one query, then lazily loading a relation per parent with N more. It shows up as many identical spans in a trace, a very high call count for a primary-key lookup in `pg_stat_statements`, and a query count that grows with page size in tests.

**`select_related` vs `prefetch_related`?**
`select_related` follows forward foreign keys and one-to-ones with a SQL JOIN in a single query. `prefetch_related` handles to-many relations with a second `IN (...)` query and joins the results in Python, which avoids duplicating parent rows.

**Why is `OFFSET` pagination slow and what do you use instead?**
The database must produce and discard every skipped row, so page k costs `O(k × page size)`. Keyset pagination filters on the last seen sort key (`(created_at, id) < (?, ?)`) with a matching index, so every page costs the same. You lose random page access.

**How do you order columns in a composite index?**
Equality predicates first, then the range predicate or the `ORDER BY` column; columns after a range can't narrow the scan. Lead with the most commonly filtered column, usually `tenant_id` in a multi-tenant schema.

**Why is `count(*)` slow on a big PostgreSQL table?**
MVCC means visibility is per row and per transaction, so there's no stored count. The database scans the heap or an index plus the visibility map. Use estimates from `pg_class.reltuples`, precomputed rollups, or avoid exact totals in the UI.

**How does DataLoader fix N+1 in GraphQL?**
Each resolver calls `load(key)`, which only enqueues the key and returns a future. At the end of the event-loop tick one batch function loads all keys with a single `IN` query. Results are memoized per request, so repeated keys cost nothing. Loaders must be request-scoped to avoid leaking data across users.

## Further reading

- PostgreSQL documentation: "Using EXPLAIN" in the Performance Tips chapter, and the `pg_stat_statements` and `auto_explain` module pages.
- Markus Winand, *SQL Performance Explained* (also online as "Use The Index, Luke").
- Django documentation: "Database access optimization" and the `QuerySet` API reference for `select_related`, `prefetch_related` and `Prefetch`.
- The `graphql/dataloader` repository README, which defines the batching and caching semantics.
- Egor Rogov, *PostgreSQL 14 Internals*, for how the planner, statistics and MVCC interact.
