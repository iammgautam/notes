---
title: pgvector
section: postgresql
order: 12
summary: Store embeddings in Postgres, pick a distance operator, choose and tune HNSW or IVFFlat, and avoid the filtering-plus-ANN traps that silently drop results.
---

pgvector turns Postgres into a vector store good enough for most RAG systems: embeddings sit next to the chunks, tenant IDs, permissions and metadata they belong to, and you query them with SQL in the same transaction. The hard parts aren't storing vectors; they're approximate indexes, recall you can't see, and filters that interact badly with nearest-neighbor search.

## How it works

### Types

| Type | Storage | Max dims (indexable) | Use |
|---|---|---|---|
| `vector(n)` | 4 bytes/dim (float32) | 2,000 | Default |
| `halfvec(n)` | 2 bytes/dim (float16) | 4,000 | Halve memory, small recall cost |
| `sparsevec(n)` | non-zeros only | 1,000 non-zeros | SPLADE / sparse embeddings |
| `bit(n)` | 1 bit/dim | 64,000 | Binary quantization, re-rank after |

A 1536-dim `vector` is about 6 KB per row. 10M chunks is about 60 GB of raw vectors before indexes. That number drives most design decisions.

### Distance operators

| Operator | Distance | Index opclass |
|---|---|---|
| `<->` | L2 (Euclidean) | `vector_l2_ops` |
| `<#>` | Negative inner product | `vector_ip_ops` |
| `<=>` | Cosine distance | `vector_cosine_ops` |
| `<+>` | L1 (taxicab) | `vector_l1_ops` |
| `<~>` / `<%>` | Hamming / Jaccard | `bit_hamming_ops` / `bit_jaccard_ops` |

The index is only used when the query's `ORDER BY` operator matches the index opclass, and the query has `ORDER BY ... LIMIT`. For normalized embeddings (OpenAI, most sentence-transformers), cosine, inner product and L2 rank identically; inner product is cheapest.

### Exact vs approximate

Without an index, `ORDER BY embedding <=> $1 LIMIT 10` scans every row: exact, 100% recall, linear time. Fine up to roughly 100k rows per filter scope. Beyond that, use an approximate nearest neighbor (ANN) index.

### HNSW

A layered proximity graph. Search enters at the top sparse layer, greedily walks toward the query, and descends to denser layers, keeping a candidate list of size `ef_search`.

- Build params: `m` (links per node, default 16), `ef_construction` (candidate list during build, default 64).
- Query param: `hnsw.ef_search` (default 40). Higher means better recall, slower queries. Returns at most `ef_search` rows.
- Pros: best recall/latency trade-off, no training step, handles inserts well.
- Cons: slow to build, large (often bigger than the vectors), wants to fit in memory.

### IVFFlat

Partitions vectors into `lists` clusters with k-means at build time. Queries scan the `ivfflat.probes` nearest clusters.

- Build after data exists; clusters don't adapt to later inserts, so recall drifts as data changes. Rebuild periodically.
- Rule of thumb: `lists = rows / 1000` up to 1M rows, `sqrt(rows)` beyond; `probes` around `sqrt(lists)` to start.
- Pros: fast build, smaller. Cons: lower recall at the same speed, needs retraining.

> **Rule of thumb:** Default to HNSW. Use IVFFlat only when build time or memory is the binding constraint.

## In practice

### Schema for multi-tenant RAG

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE document_chunks (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id   bigint NOT NULL,
  document_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index int NOT NULL,
  content     text NOT NULL,
  metadata    jsonb NOT NULL DEFAULT '{}',
  embedding   vector(1536) NOT NULL,
  model       text NOT NULL,          -- embedding model + version
  created_at  timestamptz NOT NULL DEFAULT now()
);

SET maintenance_work_mem = '8GB';
SET max_parallel_maintenance_workers = 7;
CREATE INDEX CONCURRENTLY chunks_embedding_hnsw
  ON document_chunks USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE INDEX CONCURRENTLY chunks_tenant_idx ON document_chunks (tenant_id);
```

HNSW builds are much faster when the graph fits in `maintenance_work_mem`; the server logs a notice when it doesn't.

### Query

```sql
SET LOCAL hnsw.ef_search = 100;

SELECT id, document_id, content, embedding <=> $1 AS distance
FROM document_chunks
WHERE tenant_id = $2
ORDER BY embedding <=> $1
LIMIT 10;
```

In Django with `pgvector-python`:

```python
from pgvector.django import CosineDistance

(Chunk.objects
    .filter(tenant_id=tid)
    .annotate(distance=CosineDistance("embedding", query_vec))
    .order_by("distance")[:10])
```

## Filtering + ANN pitfalls

This is where most production RAG bugs live.

**Post-filtering loses results.** The HNSW scan returns its top `ef_search` candidates by distance, then Postgres applies `WHERE tenant_id = 42`. If tenant 42 owns 1% of rows, on average only 0.4 of the 40 candidates survive. You asked for 10 and get 0–2, with no error.

Fixes, roughly in order of preference:

1. **Iterative index scans** (pgvector 0.8+). The scan keeps going until enough rows pass the filter:

   ```sql
   SET hnsw.iterative_scan = relaxed_order;   -- or strict_order
   SET hnsw.max_scan_tuples = 20000;          -- safety cap
   ```

   `relaxed_order` may return slightly out-of-order results; wrap in a subquery and re-sort by distance if you need strict order.

2. **Partial index per large tenant or category**, when the filter has few distinct values:

   ```sql
   CREATE INDEX CONCURRENTLY chunks_hnsw_tenant_42
     ON document_chunks USING hnsw (embedding vector_cosine_ops)
     WHERE tenant_id = 42;
   ```

3. **Partition by tenant** (or tenant bucket), with an HNSW index per partition. Partition pruning sends the query to a small index.

4. **Exact search for small scopes.** If the filter leaves 5k rows, skip the ANN index entirely; a B-tree on `tenant_id` plus exact distance on 5k vectors is fast and has perfect recall. The planner may do this on its own; check with `EXPLAIN`.

5. **Raise `ef_search`** as a blunt instrument; costs latency for everyone.

> **Gotcha:** Low recall is invisible. Your RAG answers just get worse. Measure recall offline: for a sample of queries, compare ANN top-k with exact top-k (`SET enable_indexscan = off`) and track recall@k per tenant size bucket.

## Tuning ef_search and friends

| Knob | Effect | Starting point |
|---|---|---|
| `hnsw.ef_search` | Recall vs latency per query | 40; try 100–200 for RAG |
| `m` | Graph connectivity, index size | 16; 32 for high-dim / high recall |
| `ef_construction` | Build quality vs build time | 64; 128–200 if recall plateaus |
| `ivfflat.probes` | Clusters scanned | `sqrt(lists)` |
| `halfvec` / `bit` + re-rank | Memory | When the index won't fit in RAM |

Set `ef_search` with `SET LOCAL` inside the transaction so it doesn't leak through a pooled connection.

### Quantization and re-ranking

Index a smaller representation, then re-rank exact on the shortlist:

```sql
CREATE INDEX ON document_chunks
  USING hnsw ((binary_quantize(embedding)::bit(1536)) bit_hamming_ops);

SELECT id FROM (
  SELECT id, embedding FROM document_chunks
  WHERE tenant_id = $2
  ORDER BY binary_quantize(embedding)::bit(1536) <~> binary_quantize($1)
  LIMIT 200
) shortlist
ORDER BY embedding <=> $1
LIMIT 10;
```

## Pitfalls

- **Index doesn't fit in memory.** HNSW latency falls off a cliff when graph pages come from disk. Watch `shared_buffers` hit ratio on the index; use `halfvec` or quantization.
- **Mixing embedding models.** Vectors from different models are not comparable. Store `model` and filter on it; re-embed on model change into a new column, build the index, then switch.
- **Bulk inserts into HNSW** are slow (graph inserts). For initial loads, insert first, build the index after.
- **Opclass mismatch.** Index with `vector_cosine_ops`, query with `<->`: seq scan.
- **No `LIMIT`.** ANN indexes are only used for `ORDER BY distance LIMIT k`.
- **Hybrid search done badly.** Combine full-text and vector scores with Reciprocal Rank Fusion rather than adding raw scores on different scales.

## Checklist

- HNSW with the opclass matching your distance operator.
- `SET LOCAL hnsw.ef_search` per query; enable iterative scans on pgvector 0.8+.
- Handle selective filters with partial indexes, partitions, or exact search.
- Measure recall@k against exact search, per filter selectivity.
- Track embedding model per row.
- Keep the HNSW index in memory, or quantize.

## Interview angle

**Q: HNSW vs IVFFlat?**
HNSW is a navigable graph: better recall/latency, no training, handles inserts, but slower to build and larger. IVFFlat clusters with k-means: fast build, smaller, but needs data up front and loses recall as data drifts.

**Q: Why does a filtered vector query return fewer rows than `LIMIT`?**
The ANN index returns `ef_search` nearest candidates, and the filter is applied afterward. Selective filters discard most of them. Use iterative scans, partial indexes, partitioning, or exact search.

**Q: When is pgvector enough versus a dedicated vector DB?**
Up to tens of millions of vectors with moderate QPS, especially when you need transactional consistency with relational data and tenant filters. Dedicated systems win at hundreds of millions of vectors or very high QPS.
