---
title: GIN, GiST and BRIN
section: postgresql
order: 5
summary: What the non-B-tree index types store, which operators they serve, their write and size costs, and how to pick one.
---

B-tree handles scalar equality and ranges. Once you query inside values (JSONB containment, array membership, full-text tokens, fuzzy text) or across geometric and range types, or you want a tiny index over an append-only time series, you need a different access method. Picking the wrong one usually means either an unused index or a write path that slows to a crawl.

## How it works

### GIN (generalized inverted index)

GIN maps each **element** inside a value to the list of rows containing it, like the index at the back of a book.

```text
document row 1: tsvector 'postgr':1 'queue':2
document row 2: tsvector 'queue':1 'redis':3

GIN:  'postgr' -> [row 1]
      'queue'  -> [row 1, row 2]
      'redis'  -> [row 2]
```

- One row produces many index entries (one per lexeme, JSONB key/value, array element, trigram).
- Lookups are fast for "contains X" and "contains X and Y".
- Inserts are expensive because one row touches many posting lists. To soften this, GIN has a **pending list** (`fastupdate = on` by default): new entries go to an unsorted list and are merged in bulk by vacuum, by `gin_clean_pending_list()`, or when it exceeds `gin_pending_list_limit` (4 MB default). Searches must also scan the pending list, so a large one slows reads.
- Results are often lossy and rechecked against the heap.

Common operator classes:

| Opclass | Data | Operators |
|---|---|---|
| `jsonb_ops` (default) | JSONB | `@>`, `?`, `?&`, `@?`, `@@` (and any-key) |
| `jsonb_path_ops` | JSONB | `@>`, `@?`, `@@` only; smaller, faster |
| `array_ops` | arrays | `@>`, `<@`, `&&`, `=` |
| `tsvector_ops` | full-text | `@@` |
| `gin_trgm_ops` (pg_trgm) | text | `LIKE`, `ILIKE`, `~`, `%`, `=` |

### GiST (generalized search tree)

GiST is a balanced tree where each internal entry holds a **bounding predicate** that covers everything below it (a bounding box, a range, a signature). Search descends into every child whose predicate might match.

- Supports overlap, containment, adjacency and **nearest-neighbor ordering** (`ORDER BY col <-> point LIMIT k`).
- Used for: PostGIS geometry, range types (`tstzrange`, `int4range`), exclusion constraints, `ltree`, trigram similarity with KNN.
- Lossy; rechecks against the heap.
- Cheaper to update than GIN, usually slower to query for pure containment.

The killer feature is the **exclusion constraint**, which B-tree can't express:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE room_bookings (
  id        bigserial PRIMARY KEY,
  tenant_id bigint NOT NULL,
  room_id   bigint NOT NULL,
  during    tstzrange NOT NULL,
  EXCLUDE USING gist (tenant_id WITH =, room_id WITH =, during WITH &&)
);
```

No two bookings for the same room may overlap. `btree_gist` lets scalar `=` participate in a GiST index.

SP-GiST is a sibling for space-partitioned structures (quad-trees, radix trees); useful for IP ranges (`inet`) and points with non-uniform distribution.

### BRIN (block range index)

BRIN stores a **summary per range of heap pages** (default 128 pages, about 1 MB), typically the min and max of the column.

```text
pages 0-127:    created_at min=2026-01-01 max=2026-01-03
pages 128-255:  created_at min=2026-01-03 max=2026-01-06
...
```

A query for January 4th skips every block range whose min/max can't contain it, then scans the candidate ranges fully.

- Tiny: a BRIN index on a 500 GB table can be a few MB.
- Near-zero write overhead.
- Only works if the column **correlates with physical order**. Append-only `created_at` on an events table is the textbook case. Check `pg_stats.correlation`; you want close to 1 or -1.
- `minmax_multi_ops` (PG 14+) stores several ranges per block range, which tolerates some out-of-order inserts.
- New pages are summarized by autovacuum or `brin_summarize_new_values()`; set `autosummarize = on` to summarize on fill.

## Choosing

| Need | Index |
|---|---|
| Equality, range, sort on scalar | B-tree |
| JSONB containment `@>` | GIN `jsonb_path_ops` |
| JSONB key existence `?` | GIN `jsonb_ops` |
| Array membership / overlap | GIN |
| Full-text `@@` | GIN on `tsvector` |
| `ILIKE '%foo%'`, fuzzy match | GIN `gin_trgm_ops` (GiST if you need KNN by similarity) |
| Ranges overlap, no-overlap constraint | GiST (+ `btree_gist`) |
| Geospatial | GiST (PostGIS) |
| Huge append-only time series, range scans | BRIN on timestamp |
| Vector similarity | HNSW / IVFFlat (pgvector) |

## In practice

### GIN on JSONB attributes

```sql
CREATE INDEX CONCURRENTLY events_props_gin
  ON events USING gin (properties jsonb_path_ops);

SELECT id FROM events
WHERE tenant_id = 42
  AND properties @> '{"source": "stripe", "status": "failed"}';
```

The planner can combine this with a B-tree on `tenant_id` via `BitmapAnd`. To put `tenant_id` inside the GIN index, use `btree_gin`:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE INDEX CONCURRENTLY events_tenant_props_gin
  ON events USING gin (tenant_id, properties jsonb_path_ops);
```

### Trigram search for "contains" queries

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY customers_name_trgm
  ON customers USING gin (name gin_trgm_ops);

SELECT id, name FROM customers
WHERE tenant_id = 42 AND name ILIKE '%acme%';
```

Patterns need at least 3 characters to extract trigrams; `ILIKE '%ab%'` falls back to a full index or heap scan.

### BRIN on an events table

```sql
CREATE INDEX events_created_brin
  ON events USING brin (created_at) WITH (pages_per_range = 64, autosummarize = on);

SELECT count(*) FROM events
WHERE created_at >= '2026-10-01' AND created_at < '2026-10-02';
```

In a multi-tenant table where queries always filter by tenant first, BRIN alone helps global time scans (retention jobs, rollup builders), not per-tenant reads.

## Pitfalls

- **GIN write amplification.** Bulk inserting into a table with several GIN indexes can be 5–10x slower than without. For large loads, drop and recreate, or batch and let the pending list absorb it.
- **Pending list latency spikes.** A user insert that triggers a pending list flush pays for merging megabytes of entries. If you see periodic slow inserts, lower `gin_pending_list_limit` or set `fastupdate = off` on that index and let vacuum handle cleanup.
- **Default `jsonb_ops` is big.** It indexes every key and value separately. If you only use `@>`, `jsonb_path_ops` is often 2–3x smaller and faster.
- **GIN doesn't help `->>` equality.** `properties->>'plan' = 'pro'` can't use GIN; rewrite to `properties @> '{"plan":"pro"}'` or add an expression B-tree index.
- **BRIN on unordered data.** After updates, `CLUSTER`, or backfills with old timestamps, correlation drops and BRIN scans most of the table. Check `correlation` before choosing it.
- **No index-only scans** for GIN or BRIN; the heap is always visited.

> **Rule of thumb:** GIN for "what's inside the value", GiST for "how values relate in space or time", BRIN for "where in the table is this time range", B-tree for everything else.

## Checklist

- Match the opclass to the operators your queries actually use.
- Prefer `jsonb_path_ops` unless you need `?` key-existence queries.
- Use `btree_gist` exclusion constraints for no-overlap rules instead of app-level checks.
- Verify `pg_stats.correlation` before relying on BRIN.
- Monitor insert latency after adding GIN indexes to hot tables.

## Interview angle

**Q: Why is GIN slow to update?**
One row produces many index entries, one per element. Each insert touches many posting lists. The pending list batches this but shifts cost to vacuum and occasional flushes.

**Q: When is BRIN a good idea?**
Very large tables where the indexed column tracks physical insert order, like timestamps on append-only logs. It is tiny and cheap to maintain but useless on randomly ordered data.

**Q: How do you prevent double-booking in Postgres?**
An exclusion constraint using GiST on a range column with `&&`, plus `btree_gist` for the scalar equality columns.
