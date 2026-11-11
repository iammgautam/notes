---
title: B-tree indexes
section: postgresql
order: 3
summary: How B-tree indexes are laid out, how to order composite columns, when to use INCLUDE for index-only scans, and why indexes get ignored.
---

B-tree is the default index type and the one that does most of the work in an OLTP database. Getting column order right in composite indexes is usually worth more than any other single optimization, and most index bloat in production comes from indexes nobody needed.

## How it works

A Postgres B-tree is a Lehman-Yao style balanced tree of 8 KB pages:

```text
              [root]
             /      \
      [internal]   [internal]
      /   |   \       |    \
   [leaf][leaf][leaf][leaf][leaf]  <-> sibling links
     |
   (key, TID) entries -> heap tuple at (block, offset)
```

- Leaf entries hold the indexed key(s) and a **TID** pointing at the heap tuple.
- Leaves are linked, so range scans walk sideways without going back up.
- Depth is small: a table of 100M rows typically has a B-tree 3–4 levels deep. A lookup touches 3–4 index pages plus the heap page.
- Since PG 13, **deduplication** stores one copy of repeated keys with a list of TIDs, which shrinks indexes on low-cardinality columns like `tenant_id` or `status` substantially.
- Since PG 14, **bottom-up deletion** removes index entries for dead versions created by non-HOT updates before a page splits, reducing bloat.

B-trees support `=`, `<`, `<=`, `>`, `>=`, `BETWEEN`, `IN`, `IS NULL`, and prefix `LIKE 'abc%'` (with `text_pattern_ops` or the C collation). They also return rows in sorted order, which lets `ORDER BY ... LIMIT` stop early.

### Composite indexes and column order

A composite index on `(a, b, c)` is sorted by `a`, then `b` within `a`, then `c` within `b`. It is like a phone book sorted by last name, then first name.

It can efficiently serve:

- `a = ?`
- `a = ? AND b = ?`
- `a = ? AND b = ? AND c > ?`
- `a = ? ORDER BY b`

It cannot efficiently serve `b = ?` alone. PG 18 adds skip scan for some of these cases when `a` has few distinct values, but on 16/17 you should assume the leading column must be constrained.

The ordering rule:

1. **Equality columns first.** `tenant_id`, `status`, `event_type`.
2. **Then the range or sort column.** `created_at`, `id`.
3. Only one range column benefits from tree navigation; columns after a range predicate are checked inside the index but do not narrow the scan.

> **Rule of thumb:** Equality, then sort, then range. In a multi-tenant schema, `tenant_id` is nearly always the first column.

### Covering indexes with INCLUDE

`INCLUDE` adds payload columns to leaf entries without making them part of the key. They cannot be searched or sorted on, but they let the planner answer the query from the index alone.

```sql
CREATE INDEX orders_tenant_created_incl_idx
  ON orders (tenant_id, created_at DESC)
  INCLUDE (status, total_cents);
```

### Index-only scans and the visibility map

An index does not store visibility information. To skip the heap, Postgres checks the **visibility map**: one bit per heap page saying "every tuple here is visible to everyone". If the bit is set, no heap visit is needed. If not, it fetches the heap page anyway and reports it as `Heap Fetches`.

Vacuum sets visibility-map bits. On a write-heavy table that autovacuum rarely visits, an "index-only" scan may do as much heap IO as a normal index scan.

## In practice

A typical multi-tenant list endpoint:

```sql
-- GET /api/orders?status=open, newest first, 50 per page
SELECT id, created_at, status, total_cents
FROM orders
WHERE tenant_id = $1 AND status = 'open'
ORDER BY created_at DESC, id DESC
LIMIT 50;
```

The matching index lets Postgres jump to the right slice and read 50 entries in order, no sort node:

```sql
CREATE INDEX CONCURRENTLY orders_tenant_status_created_idx
  ON orders (tenant_id, status, created_at DESC, id DESC);
```

Keyset pagination for the next page uses the same index:

```sql
SELECT id, created_at, status, total_cents
FROM orders
WHERE tenant_id = $1 AND status = 'open'
  AND (created_at, id) < ($2, $3)
ORDER BY created_at DESC, id DESC
LIMIT 50;
```

Unique constraints are B-tree indexes too. In a multi-tenant table, uniqueness is almost always per tenant:

```sql
ALTER TABLE projects
  ADD CONSTRAINT projects_tenant_slug_key UNIQUE (tenant_id, slug);
```

PG 15+ supports `UNIQUE NULLS NOT DISTINCT` when you want at most one row with `NULL`.

### Finding unused and duplicate indexes

Every index slows writes, costs WAL, takes vacuum time and consumes cache. Audit them:

```sql
SELECT s.relname AS table, s.indexrelname AS index,
       s.idx_scan, pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0 AND NOT i.indisunique
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

An index on `(tenant_id)` is redundant if `(tenant_id, created_at)` exists, since the composite serves both. Check replicas too: `idx_scan` is per server, and an index unused on the primary may serve read-replica queries.

## When indexes aren't used

| Cause | Example | Fix |
|---|---|---|
| Low selectivity | `status = 'active'` matches 90% | Expected. Partial index on the rare value instead. |
| Function on column | `WHERE lower(email) = ?` | Expression index on `lower(email)`. |
| Type mismatch | `bigint_col = '42'::numeric` | Cast the parameter, not the column. |
| Leading column missing | Index `(tenant_id, created_at)`, query on `created_at` only | Add a separate index or reorder. |
| Leading wildcard | `LIKE '%foo'` | Trigram GIN index. |
| `OR` across columns | `a = 1 OR b = 2` | BitmapOr with two indexes, or `UNION`. |
| Collation | `LIKE 'abc%'` under a non-C collation | `text_pattern_ops` index. |
| Stale stats | New bulk data, `reltuples` wrong | `ANALYZE`. |
| Small table | 200 rows | Seq scan is genuinely cheaper. |

## Pitfalls

- **Indexing every foreign key blindly.** Index FKs you join on or that get `ON DELETE CASCADE` from a big parent; skip the rest.
- **Low-cardinality single-column indexes.** An index on `is_deleted` alone is almost never used. Fold it into a partial index predicate.
- **Too many indexes on hot tables.** Each `UPDATE` that changes an indexed column, or that can't be HOT, writes to every index. HOT updates only happen when no indexed column changes and the page has free space; set `fillfactor = 90` on update-heavy tables to leave room.
- **Bloat.** Check with `pgstattuple` or estimation queries; rebuild online with `REINDEX INDEX CONCURRENTLY`.
- **Descending vs ascending.** A B-tree can be scanned backward, so `DESC` only matters for mixed directions like `ORDER BY a ASC, b DESC`.

> **In production:** Watch `n_tup_hot_upd / n_tup_upd` in `pg_stat_user_tables`. A low HOT ratio on an update-heavy table often means an index on a frequently updated column like `updated_at` or `last_seen_at`.

## Checklist

- Start composite indexes with `tenant_id`, then equality filters, then the sort/range column.
- Match `ORDER BY` columns and direction to avoid sort nodes on paginated endpoints.
- Add `INCLUDE` only for hot queries where index-only scans pay off.
- Always `CREATE INDEX CONCURRENTLY` on live tables.
- Audit `idx_scan = 0` indexes quarterly, checking replicas first.
- Keep autovacuum healthy so index-only scans stay index-only.

## Interview angle

**Q: Index on `(a, b)`; does `WHERE b = 1` use it?**
Generally no on PG 16/17: the index is sorted by `a` first. Postgres may full-scan the index if it's much smaller than the table, but it can't seek. PG 18 skip scan can help when `a` has few distinct values.

**Q: What is a covering index?**
An index that contains every column the query needs, via key columns or `INCLUDE`, enabling an index-only scan that skips the heap when the visibility map allows.

**Q: Why might an index-only scan still hit the heap?**
Pages not marked all-visible in the visibility map; vacuum hasn't run since recent writes.
