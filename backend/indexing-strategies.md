---
title: Indexing strategies and composite indexes
section: backend
order: 12
phase: "Phase 2 · Databases"
summary: Pick the right index type, use covering, partial, expression and unique indexes, order composite columns correctly, and know what each index costs.
---

An index is a second copy of some columns, organized for search, with pointers back to the rows. It turns an `O(n)` scan into an `O(log n)` lookup and can return rows already sorted. You pay for it on every insert, many updates, in WAL, vacuum time and cache memory. Most slow endpoints in a Django/Postgres backend come down to a missing index, a composite index with columns in the wrong order, or an index the planner can't use because the query doesn't match it.

## How an index lookup works

```text
                    root  [ 1000 | 5000 ]
                 /            |            \
       [ 200 | 600 ]   [ 2000 | 3500 ]   [ 7000 | 9000 ]      internal
        /   |   \            ...               ...
 leaf: [k,tid][k,tid] <-> [k,tid][k,tid] <-> ...    sorted, linked
          |                   |
          v                   v
 heap: page 812, item 4   page 37, item 9         random reads
```

A PostgreSQL B-tree page is 8 KB and holds hundreds of keys, so a billion-row index is only 4–5 levels deep. A lookup descends to a leaf, then follows sibling links for a range; each leaf entry points to a heap tuple (`ctid`) that the executor fetches. Three ways to use an index:

| Plan node | What it does | Good when |
|---|---|---|
| Index Scan | Walk index, fetch each heap row in index order | Few rows, or need index order (ORDER BY ... LIMIT) |
| Index Only Scan | Answer from the index alone | All needed columns are in the index, table mostly all-visible |
| Bitmap Index Scan + Bitmap Heap Scan | Collect matching TIDs, sort by page, read each page once | Hundreds to many thousands of rows; combining indexes |

## Indexing strategies

### B-tree

The default and the right answer most of the time. Supports `=`, `<`, `<=`, `>`, `>=`, `BETWEEN`, `IN`, `IS NULL`, `ORDER BY` in either direction, and prefix `LIKE 'abc%'` (with the `C` collation or `text_pattern_ops`). Composite B-trees are sorted by the first column, then the second within it, and so on. PostgreSQL 13+ deduplicates repeated keys, which keeps indexes on low-cardinality columns smaller. Details in [B-tree indexes]({{ '/postgresql/btree-indexes/' | relative_url }}).

### Hash

Stores a 32-bit hash of the key and supports only `=`; WAL-logged and crash-safe since PostgreSQL 10. Single-column, never unique, no ranges or sorting. It can be smaller than a B-tree for long keys (URLs, tokens), but rarely enough to justify the restrictions.

### GIN

A generalized inverted index: for each element (array item, JSONB key or path, text lexeme, trigram), a sorted list of rows that contain it. Serves "contains" queries:

- `tags @> ARRAY['urgent']` and `&&` on arrays
- `attributes @> '{"color": "red"}'` on JSONB (`jsonb_path_ops` is smaller but only supports `@>`-style operators)
- `search_vector @@ to_tsquery(...)` for full-text search
- `name ILIKE '%lamp%'` and similarity with `pg_trgm`

Reads are fast; writes are expensive because one row adds many entries. `fastupdate` buffers them in a pending list (flushed by vacuum or past `gin_pending_list_limit`, 4 MB by default), smoothing writes but slowing searches until the flush.

### GiST and SP-GiST

GiST is a balanced tree of possibly overlapping bounding predicates, behind PostGIS, range overlap (`&&`), exclusion constraints, `pg_trgm` and nearest-neighbor ordering (`ORDER BY location <-> point LIMIT 10`). Matches may be lossy and rechecked. SP-GiST partitions space without overlap (quad-trees, k-d trees, radix tries) and suits points and IP prefixes.

### BRIN

Block range index: for each range of table pages (128 by default), a summary such as min and max; queries skip ranges that can't match. On a 1 TB append-only table it can be a few megabytes. It only works when physical order correlates with the column (`pg_stats.correlation` near 1 or -1), as with `created_at` on insert-only tables. See [GIN, GiST and BRIN]({{ '/postgresql/gin-gist-brin/' | relative_url }}).

### Choosing an index type

| Query shape | Index |
|---|---|
| Equality, ranges, sorting, prefix match | B-tree |
| Equality only on long keys | B-tree (hash if measured smaller and faster) |
| Array/JSONB containment, full-text, `%substring%` | GIN |
| Overlap of ranges, geometry, nearest neighbor, no-overlap constraints | GiST |
| Huge append-only table filtered by time | BRIN |
| Vector similarity | HNSW or IVFFlat ([pgvector]({{ '/postgresql/pgvector/' | relative_url }})) |

### Covering indexes

A covering index contains every column the query needs, so the planner can use an Index Only Scan and skip the heap. In PostgreSQL 11+ you add non-key payload columns with `INCLUDE`:

```sql
CREATE INDEX orders_tenant_created_cov
  ON orders (tenant_id, created_at DESC) INCLUDE (status, total_cents);
```

`INCLUDE` columns aren't part of the sort order and can't be searched on, but they ride along in the leaf. Index-only scans still check the visibility map: if vacuum hasn't marked a page all-visible, Postgres visits the heap anyway (`Heap Fetches` in `EXPLAIN ANALYZE`). Keep autovacuum healthy on tables that rely on them.

In MySQL InnoDB the table is clustered by primary key and every secondary index stores the PK, so a large or random PK bloats every secondary index.

### Partial indexes

A partial index only contains rows matching a `WHERE` clause:

```sql
CREATE INDEX jobs_pending_idx ON jobs (run_at) WHERE status = 'pending';
```

If 99% of jobs are done, the index is 1% of the size and stays in cache. The planner uses it only when it can prove the query's `WHERE` implies the index predicate: `status = 'pending'` literally, not `status = $1` on a generic plan. Partial **unique** indexes express rules like "one active subscription per tenant". More in [Partial and expression indexes]({{ '/postgresql/partial-and-expression-indexes/' | relative_url }}).

### Expression indexes

Index the result of an expression when queries filter on that expression:

```sql
CREATE UNIQUE INDEX users_email_lower_key ON users (lower(email));
-- used by:  WHERE lower(email) = lower($1)
-- not by:   WHERE email = $1
```

The query must use the same expression; `ANALYZE` also gathers statistics on it. Common uses: case-insensitive lookups, a JSONB key, date truncation.

### Unique indexes

A unique index is the only race-free way to enforce uniqueness; in Postgres only B-tree supports it. A `UNIQUE` constraint creates one underneath; use the constraint when a foreign key references the columns or it must be deferrable, the index form for an expression or `WHERE` clause. By default `NULL`s are distinct (many rows may have `NULL`); PostgreSQL 15+ supports `NULLS NOT DISTINCT`. In multi-tenant tables, uniqueness is almost always `(tenant_id, ...)`.

### The write cost of indexes

Every index adds work to every write:

- **Inserts** add one entry per index; GIN can add dozens.
- **Updates** create a new row version. If no indexed column changed and it fits on the same page, it's a HOT (heap-only tuple) update that skips indexes. Index a hot column (`updated_at`, `last_seen_at`) and every update writes every index.
- **WAL and replication:** more index writes, more WAL, more replica lag.
- **Vacuum:** each index must be scanned to remove dead entries.
- **Memory:** indexes compete with table data for `shared_buffers` and page cache.

A write-heavy table with 15 indexes can spend more I/O on indexes than on the table.

### How the planner picks an index

PostgreSQL's planner is cost-based. For each candidate plan it estimates how many rows each step returns and what that costs, then picks the cheapest.

1. **Statistics.** `ANALYZE` samples each column into `pg_statistic` (readable via `pg_stats`): null fraction, number of distinct values, most common values and their frequencies, a histogram, and the physical-order correlation. `default_statistics_target` (100) controls the sample size.
2. **Selectivity.** From those, the planner estimates the fraction of rows each predicate matches. By default it assumes columns are independent, which goes wrong for correlated columns (`city` and `zip_code`); `CREATE STATISTICS` fixes that with multi-column statistics.
3. **Cost.** Sequential page reads cost `seq_page_cost` (1.0), random reads `random_page_cost` (4.0, often lowered to about 1.1 on SSDs), plus small CPU costs per tuple and per operator. `effective_cache_size` tells it how much is likely cached.
4. **Choice.** Few rows: index scan. A moderate fraction: bitmap scan. A large fraction (often beyond roughly 5–20% of the table, depending on correlation and caching): sequential scan, because random reads per row would cost more than reading everything in order.

So "the index exists but isn't used" means the query doesn't match it (function, cast, wrong leading column), the estimate says too many rows, or statistics are stale. Use [EXPLAIN ANALYZE]({{ '/postgresql/explain-analyze/' | relative_url }}) and compare estimated vs actual rows. See also [How Postgres executes a query]({{ '/postgresql/query-execution/' | relative_url }}).

## Composite indexes

### Column order and the leftmost prefix

A composite index on `(a, b, c)` is sorted by `a`, then `b` within each `a`, then `c`. That means it can seek on any **leftmost prefix**:

| Predicate | Uses `(a, b, c)` for a seek? |
|---|---|
| `a = 1` | Yes |
| `a = 1 AND b = 2` | Yes |
| `a = 1 AND b = 2 AND c > 5` | Yes, all three |
| `b = 2` | No seek; at best a full index scan |
| `a = 1 AND c = 3` | Seeks on `a`, then filters `c` inside the index |

PostgreSQL 18 added B-tree skip scan, which can use `(a, b)` for `WHERE b = 2` when `a` has few distinct values by jumping through each `a`. Treat it as a bonus, not a design tool.

One composite index often replaces several: `(tenant_id, created_at)` serves `WHERE tenant_id = ?` too, so a separate index on `tenant_id` alone is redundant.

### Equality first, then range

Once the index meets a range condition, the columns after it no longer narrow the scanned slice. Put columns tested with equality first, and the range column last:

```text
WHERE tenant_id = 7 AND created_at BETWEEN 2000 AND 2500

 index (tenant_id, created_at)        index (created_at, tenant_id)
 (6, 2400) (6, 2600)                  (2000, 3) (2000, 7) (2000, 51)
 (7, 1900)                            (2001, 7) (2001, 88) (2002, 12)
 (7, 2000) ┐                          ...
 (7, 2210) │ one contiguous slice     every tenant between 2000 and
 (7, 2500) ┘ = exactly the matches    2500 is read; ~99% thrown away
 (7, 2700)
```

"Most selective column first" is the wrong rule. Query shape decides: equality columns before range columns, and among equality columns, the one other queries also filter on alone goes first (in multi-tenant schemas, `tenant_id`).

When the query also sorts, MongoDB's documented **ESR rule** applies to any B-tree: **E**quality, then **S**ort, then **R**ange. For `WHERE tenant_id = ? AND total_cents > 10000 ORDER BY created_at DESC LIMIT 20`, an index on `(tenant_id, created_at, total_cents)` walks rows already in date order, checks the amount inside the index and stops after 20 hits. An index on `(tenant_id, total_cents, created_at)` finds the matching rows precisely but must sort all of them before returning 20. When most rows match the range, ESR wins by a lot.

### Sort elimination

If the index order matches `ORDER BY` after the equality columns, the planner skips the Sort node, and with `LIMIT` it stops reading after N entries. A B-tree can be read backward, so `(tenant_id, created_at)` serves both `ASC` and `DESC`. Mixed directions need a matching index:

```sql
-- ORDER BY created_at DESC, id ASC needs:
CREATE INDEX ON events (tenant_id, created_at DESC, id ASC);
```

Keyset pagination depends on this: `WHERE tenant_id = $1 AND (created_at, id) < ($2, $3) ORDER BY created_at DESC, id DESC LIMIT 50` with an index on `(tenant_id, created_at DESC, id DESC)` reads exactly 50 entries per page at any depth. See [Pagination]({{ '/backend/pagination/' | relative_url }}).

### Composite vs several single-column indexes

Postgres can combine single-column indexes with `BitmapAnd`/`BitmapOr`, useful for ad hoc filter combinations (admin search). It's slower than one composite for a known hot query and never provides ordering. Composites for hot queries, a few single-column indexes for the long tail.

## In practice

A multi-tenant list endpoint and its index:

```sql
-- GET /api/orders?status=open, newest first
SELECT id, created_at, status, total_cents
FROM orders
WHERE tenant_id = $1 AND status = 'open'
ORDER BY created_at DESC
LIMIT 50;

CREATE INDEX CONCURRENTLY orders_tenant_status_created_idx
  ON orders (tenant_id, status, created_at DESC);
```

What a good plan looks like (illustrative, trimmed): no Sort node, and the index condition covers both equality columns.

```text
Limit (actual rows=50 loops=1)
  -> Index Scan using orders_tenant_status_created_idx on orders
       Index Cond: ((tenant_id = 42) AND (status = 'open'::text))
Execution Time: 0.2 ms
```

The same indexes declared in Django (illustrative). Build them on large tables with `AddIndexConcurrently` from `django.contrib.postgres.operations` in a migration with `atomic = False`:

```python
from django.db import models
from django.db.models import Q


class Order(models.Model):
    tenant = models.ForeignKey("Tenant", on_delete=models.PROTECT)
    status = models.CharField(max_length=20)
    total_cents = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["tenant", "status", "-created_at"],
                name="orders_tenant_status_created_idx",
            ),
            models.Index(
                fields=["tenant", "-created_at"],
                include=["status", "total_cents"],
                name="orders_tenant_created_cov",
            ),
            models.Index(
                fields=["created_at"],
                condition=Q(status="open"),
                name="orders_open_created_idx",
            ),
        ]

```

To find indexes that cost writes but serve no reads, query `pg_stat_user_indexes` for `idx_scan = 0` on the primary and every replica; the query is in [B-tree indexes]({{ '/postgresql/btree-indexes/' | relative_url }}).

The column-order rules fall out of plain sorted-tuple behavior. This stdlib model builds two composite "indexes" as sorted lists and counts how many entries a range seek touches:

```python
import bisect
import random

INF = float("inf")


def build_index(rows, cols):
    """A composite index: (key tuple, row id) entries kept in sorted order."""
    return sorted((tuple(row[c] for c in cols), rid) for rid, row in enumerate(rows))


def seek(index, lo, hi):
    """Entries whose key is between lo and hi (tuples compare left to right)."""
    start = bisect.bisect_left(index, (lo, -1))
    end = bisect.bisect_right(index, (hi, INF))
    return index[start:end]


if __name__ == "__main__":
    rng = random.Random(42)
    rows = [{"tenant_id": rng.randrange(100), "created_at": rng.randrange(10_000)}
            for _ in range(50_000)]
    wanted = {rid for rid, r in enumerate(rows)
              if r["tenant_id"] == 7 and 2_000 <= r["created_at"] <= 2_500}

    # WHERE tenant_id = 7 AND created_at BETWEEN 2000 AND 2500

    # 1. (tenant_id, created_at): equality then range -> one tight slice.
    good = build_index(rows, ["tenant_id", "created_at"])
    hit = seek(good, (7, 2_000), (7, 2_500))
    assert {rid for _, rid in hit} == wanted
    assert len(hit) == len(wanted)               # touched only what we return
    # Sort elimination: the slice already comes out ordered by created_at.
    times = [key[1] for key, _ in hit]
    assert times == sorted(times)

    # 2. (created_at, tenant_id): range first -> every tenant in the window.
    bad = build_index(rows, ["created_at", "tenant_id"])
    touched = seek(bad, (2_000, -INF), (2_500, INF))
    assert {rid for key, rid in touched if key[1] == 7} == wanted
    assert len(touched) > 50 * len(wanted)       # ~100x more entries read

    # 3. Leftmost prefix: with no tenant_id, created_at matches are spread over
    #    all 100 tenant slices; the tightest range covers ~the whole index.
    first = bisect.bisect_left(good, ((0, 2_000), -1))
    last = bisect.bisect_right(good, ((99, 2_500), INF))
    assert last - first > 0.9 * len(good)
    print(f"rows={len(rows)} matches={len(wanted)} "
          f"good={len(hit)} bad={len(touched)}")
```

With 50,000 rows the good index touches the 34 matching entries; the reversed one touches about 2,500.

## Pitfalls

- **Function on the indexed column.** `WHERE date(created_at) = '2026-11-01'` or `WHERE lower(email) = ...` without an expression index. Fix: rewrite as a range (`created_at >= '2026-11-01' AND created_at < '2026-11-02'`) or add an expression index.
- **Leading wildcard.** `LIKE '%lamp'` can't use a B-tree. Use `pg_trgm` with GIN or full-text search.
- **Indexing everything.** Write latency and replica lag creep up; HOT update ratio (`n_tup_hot_upd / n_tup_upd`) drops. Drop unused and redundant indexes.
- **Building indexes with a plain `CREATE INDEX` on a live table.** It blocks writes for the whole build. Use `CREATE INDEX CONCURRENTLY`, and check for an `INVALID` index if it fails. See [Zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).
- **Stale or naive statistics.** After a bulk load, run `ANALYZE`. For correlated columns, add `CREATE STATISTICS`. Symptom: estimated rows off by 100x in `EXPLAIN ANALYZE`.

## Interview questions

**Q: You have an index on `(a, b)`. Which queries can use it?**
Seeks on `a = ?`, `a = ? AND b = ?`, `a = ? AND b > ?`, and ordering by `b` after `a = ?`. Not an efficient seek on `b` alone (PostgreSQL 18 skip scan may help if `a` has few values).

**Q: How do you order columns in a composite index?**
Equality columns first, then the sort column, then range columns (ESR). Among equality columns, put the one other queries also filter on alone first; in multi-tenant schemas that's `tenant_id`.

**Q: Why might Postgres ignore an index?**
The query doesn't match it (function or cast on the column, wrong leading column, leading wildcard), the planner estimates that many rows match so a sequential or bitmap scan is cheaper, or the statistics are stale or miss column correlation.

**Q: What's a covering index and when does an index-only scan still hit the heap?**
An index that contains all columns the query needs, often via `INCLUDE`. Postgres still visits the heap for pages not marked all-visible in the visibility map, so it depends on vacuum keeping up.

**Q: B-tree vs GIN vs BRIN?**
B-tree for equality, ranges and sorting on scalar values. GIN for "contains" queries on multi-valued data (arrays, JSONB, full-text, trigrams), with slower writes. BRIN for very large, append-only tables where physical order follows the column, at a tiny size.

**Q: When would you use a partial index?**
When queries target a small, well-defined subset (pending jobs, non-deleted rows, open orders) or when uniqueness applies only to a subset ("one active subscription per tenant").

## Further reading

- PostgreSQL documentation, chapter "Indexes" (index types, multicolumn, partial, expression and index-only scans).
- Markus Winand, *SQL Performance Explained* (also published online as "Use The Index, Luke").
- Lehman and Yao, "Efficient Locking for Concurrent Operations on B-Trees" (ACM TODS, 1981).
- Douglas Comer, "The Ubiquitous B-Tree" (ACM Computing Surveys, 1979).
- MongoDB documentation, "The ESR (Equality, Sort, Range) Guideline".
