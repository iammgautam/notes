---
title: Vector search and ANN indexes
section: llm
order: 4
summary: How HNSW and IVF indexes trade recall for latency, why filtering is the hard part, and when pgvector is enough versus a dedicated vector database.
---

Exact nearest-neighbor search compares the query against every vector: `O(n·d)` per query. At 10k vectors that is fine; at 10M vectors of 1,024 dimensions it is ~10 billion multiply-adds per query. Approximate nearest neighbor (ANN) indexes give up a little recall to answer in milliseconds. The engineering work is choosing the index, tuning recall against latency, and making metadata filters (tenant, permissions, document type) not destroy either.

## How it works

### HNSW

Hierarchical Navigable Small World builds a multi-layer proximity graph. Upper layers are sparse "express lanes"; the bottom layer contains every vector. A search greedily walks from an entry point toward the query, descending layers, keeping a candidate list of size `ef_search`.

```text
layer 2:  A ─────────────────── F
          │                     │
layer 1:  A ──── C ──────── E ── F
          │      │          │    │
layer 0:  A─B─── C ─ D ──── E ─G─F─H   (all vectors)
                       ▲
                     query: walk greedily, widen with ef_search
```

| Parameter | Effect | Typical |
|---|---|---|
| `m` | Edges per node. Higher = better recall, more memory, slower build | 16–32 |
| `ef_construction` | Candidate list at build time. Higher = better graph, slower build | 64–200 |
| `ef_search` | Candidate list at query time. Main recall/latency knob | 40–200 |

Properties: high recall at low latency, supports inserts without retraining, but memory-hungry (vectors plus graph) and slow to build. Performance falls off sharply once the index no longer fits in RAM.

### IVF (inverted file)

IVF clusters vectors with k-means into `lists` centroids. A query finds the nearest `probes` centroids and scans only those lists.

| Parameter | Effect | Typical starting point |
|---|---|---|
| `lists` | Number of clusters | `rows / 1000` up to 1M rows, `sqrt(rows)` beyond |
| `probes` | Clusters scanned per query | `sqrt(lists)` |

Properties: faster build and smaller memory than HNSW, but recall at a given latency is usually worse, and centroids are trained on the data at build time. If the data distribution shifts (new tenants, new content types), recall degrades until you rebuild. Do not build IVF on an empty or tiny table.

### Quantization inside the index

Dedicated engines and recent pgvector versions reduce memory by compressing vectors: scalar quantization (float32 to int8), product quantization (PQ), or binary quantization (1 bit per dimension). The common pattern is to search on compressed vectors, then **rescore** the top candidates with full-precision vectors. Done well this cuts memory 4–32x with small recall loss.

### Recall vs latency

ANN recall is measured against exact search: `recall@10` = fraction of the true top-10 that the index returned. Tuning is a curve, not a setting. For RAG, `recall@k` of 0.95+ versus exact search is usually plenty, because the embedding model's own errors are larger than the index's.

## In practice

### pgvector

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- HNSW with cosine distance; match the opclass to the query operator.
CREATE INDEX CONCURRENTLY chunk_emb_hnsw
    ON chunk_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

SET hnsw.ef_search = 100;

SELECT c.id, c.text, e.embedding <=> $1 AS distance
FROM chunk_embeddings e
JOIN chunks c ON c.id = e.chunk_id
WHERE e.tenant_id = $2
ORDER BY e.embedding <=> $1
LIMIT 20;
```

Operators: `<->` L2 distance, `<#>` negative inner product, `<=>` cosine distance. Opclasses: `vector_l2_ops`, `vector_ip_ops`, `vector_cosine_ops`. The `ORDER BY ... LIMIT` shape is what lets the planner use the index.

Practical notes:

- Raise `maintenance_work_mem` before building HNSW so the graph builds in memory; set `max_parallel_maintenance_workers` for parallel builds.
- `vector` indexes support up to 2,000 dimensions; `halfvec` up to 4,000 and halves storage.
- Check with `EXPLAIN` that you see an `Index Scan using chunk_emb_hnsw`, not a `Seq Scan` plus `Sort`.

### Filtering: the hard part

ANN indexes return the nearest `ef_search` candidates first and apply `WHERE` after. If a tenant owns 1% of rows, a top-100 candidate list might contain one or zero of their rows, and the query returns too few results.

| Strategy | How | Trade-off |
|---|---|---|
| Post-filter with iterative scan | pgvector 0.8+: `SET hnsw.iterative_scan = relaxed_order` keeps scanning until `LIMIT` is satisfied | Simple; latency grows for very selective filters |
| Partial index per large tenant | `CREATE INDEX ... WHERE tenant_id = 42` | Great recall; index count explodes with many tenants |
| Partitioning | Partition table by `tenant_id` (hash or list), one index per partition | Good isolation; more DDL to manage |
| Pre-filter then exact | For small tenants, skip the ANN index: B-tree on `tenant_id`, exact distance on a few thousand rows | Exact and fast when the filtered set is small |
| Filter-aware engine | Dedicated DBs that integrate filters into graph traversal | Extra system to run |

A pragmatic multi-tenant rule: if the filtered set is under ~10k–50k rows, exact search over that subset is fast and has perfect recall. Use ANN for large tenants or global search.

```sql
SET hnsw.iterative_scan = relaxed_order;
SET hnsw.max_scan_tuples = 20000;   -- cap work for very selective filters
```

> **Gotcha:** With `relaxed_order`, results can be slightly out of distance order. Wrap the query in a subselect and re-`ORDER BY distance` if exact order matters.

### pgvector vs a dedicated vector database

| Choose pgvector when | Choose a dedicated engine when |
|---|---|
| Up to roughly tens of millions of vectors | Hundreds of millions to billions of vectors |
| You need joins, transactions, row-level security with relational data | Vector search is the primary workload |
| One system to back up, monitor, and secure | You need built-in sharding and replication for vectors |
| Filters are tenant/ACL predicates you already index | Heavy filtered search at high QPS |
| Writes must be consistent with the source rows | You want managed quantization and tiered storage |

Starting with pgvector is rarely wrong for a SaaS product: vectors live next to the rows they describe, deletes cascade, and tenant isolation reuses existing patterns. Move when index size outgrows RAM on a reasonable instance or query load interferes with OLTP.

## Pitfalls

- **Index not in memory.** Symptom: p99 latency jumps from milliseconds to hundreds of milliseconds as data grows. Check index size with `pg_relation_size` against `shared_buffers` and RAM. Fix: `halfvec`, fewer dimensions, quantization, or a bigger box.
- **Too few results with filters.** Symptom: tenants with small data get 2 results instead of 20. Fix: iterative scan, partitioning, or exact search for small tenants.
- **Planner skips the index.** Using a distance in a `WHERE` threshold without `ORDER BY ... LIMIT`, or mismatched operator and opclass. Fix: query shape plus `EXPLAIN`.
- **IVF on drifting data.** Recall decays silently. Fix: periodic `REINDEX`, or use HNSW.
- **Heavy build blocking writes.** Fix: `CREATE INDEX CONCURRENTLY`, built off-peak, with `maintenance_work_mem` raised for that session.
- **Measuring only latency.** Fix: compute recall against exact search on a sample of queries whenever you change parameters.

## Checklist

- Default to HNSW; tune `ef_search` with a recall-vs-latency measurement.
- Match operator (`<=>`, `<#>`, `<->`) to the opclass.
- Plan filtering explicitly: iterative scan, partitions, or exact search for small subsets.
- Keep the index in RAM; use `halfvec` or reduced dimensions to get there.
- Verify plans with `EXPLAIN (ANALYZE, BUFFERS)`.

## Interview angle

- **HNSW vs IVF?** HNSW: better recall/latency and incremental inserts, more memory and slower builds. IVF: smaller and faster to build, needs training data and rebuilds when data drifts.
- **Why do filters hurt ANN?** The index finds nearest neighbors globally; selective filters discard most candidates, so you either scan more (latency) or return fewer results (recall).
- **When is brute force fine?** When the candidate set after filtering is small (thousands to tens of thousands of vectors). It is exact and often faster than a poorly filtered ANN scan.
