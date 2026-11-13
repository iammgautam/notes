---
title: Vector search and ANN indexes
section: llm
order: 14
phase: "Phase 3 · Retrieval"
summary: Exact vs approximate search, how HNSW, IVF, PQ and DiskANN trade recall, latency and memory, filtered-search traps, and pgvector vs dedicated engines.
---

Exact nearest-neighbor search compares the query against every stored vector: `O(n * d)` per query. At 100k vectors that is fine. At 10M vectors of 1,024 dimensions it is about 10 billion multiply-adds and 40 GB of memory traffic per query, which is memory-bandwidth-bound at hundreds of milliseconds to seconds even with SIMD. Approximate nearest neighbor (ANN) indexes give up a little recall to answer in single-digit milliseconds. The engineering work is choosing the index family, tuning recall against latency and memory, and making metadata filters (tenant, permissions, document type) not destroy either.

## How it works

### Exact search and the recall metric

Exact (flat, brute-force) search is the baseline everything else is measured against, and the right answer more often than people expect: a filtered subset of 20k vectors at 768 dims is 15M multiply-adds, a few milliseconds of CPU. It also has no build time, no tuning and perfect recall.

ANN quality is measured as **recall@k against exact search**: of the true top-k neighbors, what fraction did the index return. It says nothing about whether those neighbors are relevant to the user; that is the embedding model's job. For RAG, recall@k around 0.95 against exact is usually plenty, because the model's own retrieval errors are larger than the index's.

Every ANN index trades recall, latency and memory: widening the search (`ef_search`, `nprobe`) buys recall with latency, and compressing vectors buys memory with recall unless you rescore.

### HNSW

Hierarchical Navigable Small World builds a multi-layer proximity graph. Every vector lives on layer 0; each vector also gets a random top layer `l = floor(-ln(U) * mL)` with `mL = 1 / ln(M)`, so roughly a `1/M` fraction of nodes reaches layer 1, `1/M^2` reaches layer 2, and so on. Upper layers are sparse long-range "express lanes."

```text
layer 2:  A ------------------------- F
          |                           |
layer 1:  A ------- C ---------- E -- F
          |         |            |    |
layer 0:  A - B --- C - D ------ E -G- F - H     (every vector)
                        ^
             query: greedy descent on top layers,
             beam search of width ef_search on layer 0
```

**Insert**: descend greedily from the entry point to the new node's top layer, then on each layer at or below it run a beam search of width `ef_construction` to find candidates and link to up to `M` of them (`2M` on layer 0). A diversity heuristic skips a candidate that is closer to an already chosen neighbor than to the new node, which keeps long-range edges and stops clusters from becoming islands. Links are bidirectional; overfull neighbor lists are pruned.

**Search**: greedy descent with beam width 1 through the upper layers, then a beam search on layer 0 keeping the `ef_search` best candidates seen; return the best `k`. A query visits hundreds to a few thousand nodes instead of `n`.

| Parameter | Effect | Typical |
|---|---|---|
| `M` (pgvector `m`) | Links per node. Higher: better recall, more memory, slower build | 16–48 |
| `ef_construction` | Build-time beam. Higher: better graph, slower build | 64–256 |
| `ef_search` | Query-time beam. The main recall/latency knob; keep it at least `k` | 40–400 |

Properties: the best recall-latency curve of the in-memory options and incremental inserts with no training, but memory-hungry (vectors plus about `2M` neighbor ids per node on layer 0) and slow to build. Deletes leave tombstones that need repair, so heavy churn degrades the graph. Latency falls off a cliff once the graph no longer fits in RAM, because every hop becomes a random read.

### IVF

An inverted-file index runs k-means on a sample to get `nlist` centroids and assigns every vector to the list of its nearest centroid. A query compares itself to all centroids, picks the `nprobe` nearest lists and scans only those. Work per query is about `nlist + n * nprobe / nlist` distance computations.

| Parameter | Effect | Starting point (pgvector guidance) |
|---|---|---|
| `lists` / `nlist` | Number of clusters | `rows / 1000` up to 1M rows, `sqrt(rows)` beyond |
| `probes` / `nprobe` | Lists scanned per query | `sqrt(lists)` |

Recall is lost at cell boundaries: a true neighbor just across the border of an unprobed list is invisible. IVF builds faster and uses less memory than HNSW, but has a worse recall-latency curve, and centroids are frozen at build time. When the data distribution shifts (new tenants, new content types), lists become unbalanced and recall decays until you rebuild. Never build IVF on an empty or tiny table.

### Product quantization and IVF-PQ

Product quantization (PQ) compresses vectors so that billions fit in RAM:

1. Split each `d`-dim vector into `m` subvectors of `d/m` dims.
2. Run k-means with 256 centroids in each subspace; each subvector becomes a 1-byte centroid id.
3. A vector is now `m` bytes. A 1,024-dim float32 vector (4,096 bytes) with `m = 64` becomes 64 bytes, 64x smaller.

Search uses **asymmetric distance computation**: for a query, precompute an `m x 256` table of distances from each query subvector to each centroid. The distance to any stored code is then `m` table lookups and adds, which is very fast and cache-friendly. OPQ learns a rotation first so the subspaces carry balanced information.

**IVF-PQ** combines both: IVF picks the lists, PQ encodes each vector's residual from its list centroid (more accurate than encoding the raw vector). Quantization error caps recall, so production setups **refine**: take the top few hundred by PQ distance and rescore them with full vectors kept on disk or in another store.

Scalar quantization (float32 to int8, 4x) and binary quantization (1 bit per dim, 32x, Hamming distance) are simpler alternatives that many engines apply inside HNSW, again with rescoring (see [embeddings]({{ '/llm/embeddings/' | relative_url }})).

### DiskANN

DiskANN targets indexes larger than RAM. It builds a single-layer **Vamana** graph whose pruning rule (with a slack factor `alpha > 1`) deliberately keeps long-range edges, so searches converge in few hops. Full vectors and neighbor lists live together on SSD, and only PQ-compressed vectors stay in RAM to steer the beam search. Each hop is one SSD read, and the full vectors fetched along the way are used for exact reranking. The original paper served billion-point datasets from one workstation with an SSD at latencies of a few milliseconds. Streaming variants support inserts and deletes, and Timescale's pgvectorscale extension brings a DiskANN-style index to Postgres.

### Comparison

| Index | Memory per vector (1,024-d) | Build | Recall at low latency | Updates | Best for |
|---|---|---|---|---|---|
| Flat (exact) | 4 KB | none | 1.0, slow at scale | trivial | Under ~100k, filtered subsets |
| HNSW | 4 KB + links | slow | excellent | inserts fine, deletes degrade | Default up to tens of millions |
| HNSW + int8/binary + rescore | 0.1–1 KB + links | slow | very good | as HNSW | Same, memory-constrained |
| IVF-Flat | 4 KB | fast (k-means) | good | drift needs rebuild | Fast builds, batch data |
| IVF-PQ + refine | ~64–128 B in RAM | training needed | good with refine | drift needs rebuild | Hundreds of millions to billions |
| DiskANN | PQ codes in RAM, rest on SSD | slow | very good | streaming variants | Larger than RAM |

## Filtering

Real queries carry predicates: a tenant, an ACL group, a date range. ANN indexes are built over all vectors, and filters interact badly with that.

| Strategy | How | Works when | Fails when |
|---|---|---|---|
| Post-filter | ANN returns `k'` candidates, then apply `WHERE` | Filter keeps most rows | Selective filters: expected survivors are `k' * selectivity` |
| Pre-filter, exact | Resolve the filter (B-tree, bitmap), brute-force the subset | Subset is small (up to tens of thousands) | Subset is large: linear scan |
| Pre-filter, allow-list | Pass the allowed id set into the ANN search | Moderate selectivity | Very selective: graph walks through disallowed nodes |
| In-filter (filter-aware traversal) | Check the predicate during traversal; engines estimate cardinality and fall back to brute force when the subset is small | Most selectivities | Needs engine support |

With a 1% tenant and `ef_search = 40`, a post-filtered HNSW query expects 0.4 matching rows when you asked for 10. Iterative scanning (keep pulling candidates until `LIMIT` is satisfied) fixes the count but latency grows as selectivity falls. Filter-aware engines go further: some add extra graph edges per filter value, and research such as ACORN builds denser graphs so traversal stays connected under arbitrary predicates.

Multi-tenant patterns that work:

- **Small tenants: exact search** over the tenant's rows. Perfect recall, low latency, no tuning.
- **Large tenants: dedicated index** via a partial index or a partition per tenant (or a namespace or collection in a vector database).
- **Global search with selective ACLs**: iterative scan or a filter-aware engine, and measure recall per filter class, not just overall.

## In practice

### Runnable: brute force, IVF recall and post-filtering

Stdlib only: exact kNN, a toy IVF index, recall@10 against exact search as `nprobe` grows, and what post-filtering does to a 2% tenant.

```python
import heapq
import math
import random

def dot(a, b):
    return sum(x * y for x, y in zip(a, b))

def normalize(a):
    n = math.sqrt(dot(a, a))
    return [x / n for x in a]

def make_data(n, dims, clusters, rng):
    centers = [[rng.gauss(0, 1) for _ in range(dims)] for _ in range(clusters)]
    def point():
        c = rng.choice(centers)
        return normalize([x + rng.gauss(0, 0.6) for x in c])
    return [point() for _ in range(n)]

def exact_knn(q, xs, k, ids=None):
    """Brute force: score every candidate, keep the k best. O(n * d)."""
    ids = range(len(xs)) if ids is None else ids
    return heapq.nlargest(k, ids, key=lambda i: dot(q, xs[i]))

def recall_at_k(approx, exact):
    return len(set(approx) & set(exact)) / len(exact)

def kmeans(xs, k, iters, rng):
    cents = rng.sample(xs, k)
    for _ in range(iters):
        buckets = [[] for _ in range(k)]
        for x in xs:
            buckets[max(range(k), key=lambda j: dot(x, cents[j]))].append(x)
        cents = [normalize([sum(c) for c in zip(*b)]) if b else cents[j]
                 for j, b in enumerate(buckets)]
    return cents

class IVF:
    """Toy IVF-Flat: k-means coarse quantizer + exact scan of nprobe lists."""
    def __init__(self, xs, nlist, rng):
        self.xs = xs
        self.cents = kmeans(xs, nlist, 6, rng)
        self.lists = [[] for _ in range(nlist)]
        for i, x in enumerate(xs):
            self.lists[self.nearest_lists(x, 1)[0]].append(i)

    def nearest_lists(self, q, nprobe):
        return heapq.nlargest(nprobe, range(len(self.cents)),
                              key=lambda j: dot(q, self.cents[j]))

    def search(self, q, k, nprobe):
        cand = [i for j in self.nearest_lists(q, nprobe) for i in self.lists[j]]
        return exact_knn(q, self.xs, k, cand), len(cand)

if __name__ == "__main__":
    rng = random.Random(7)
    xs = make_data(3000, 24, 40, rng)
    queries = make_data(50, 24, 40, rng)
    k, nlist = 10, 32
    truth = [exact_knn(q, xs, k) for q in queries]
    ivf = IVF(xs, nlist, rng)

    results = {}
    for nprobe in (1, 2, 4, 8, nlist):
        rec = scanned = 0
        for q, t in zip(queries, truth):
            got, n = ivf.search(q, k, nprobe)
            rec += recall_at_k(got, t)
            scanned += n
        results[nprobe] = (rec / len(queries), scanned / len(queries) / len(xs))
        print(f"nprobe={nprobe:>2}  recall@10={results[nprobe][0]:.3f}"
              f"  scanned={100 * results[nprobe][1]:.0f}%")

    assert results[nlist][0] == 1.0          # probing every list is exact
    assert results[1][0] < results[8][0]     # more probes, more recall
    assert results[8][0] >= 0.9 and results[8][1] < 0.5

    # Post-filtering: a tenant owning ~2% of rows, ANN returns top-40.
    tenant = {i for i in range(len(xs)) if rng.random() < 0.02}
    post = [len([i for i in exact_knn(q, xs, 40) if i in tenant][:k])
            for q in queries]
    pre = [len(exact_knn(q, xs, k, tenant)) for q in queries]
    print(f"post-filter avg results: {sum(post) / len(post):.1f} of {k}")
    assert all(p == k for p in pre) and sum(post) / len(post) < 3
```

Output on this data: recall@10 is about 0.49 at `nprobe=1` (4% of vectors scanned), 0.83 at 4 (14%), 0.95 at 8 (27%), and 1.0 when every list is probed. Post-filtering returns under one result of the ten requested; pre-filtering with exact search returns all ten.

### Tuning loop

1. Sample 500–1,000 real queries (not synthetic ones; query distribution matters).
2. Compute exact top-k for each. In pgvector, run the query inside a transaction with `SET LOCAL enable_indexscan = off` to force an exact scan.
3. Sweep `ef_search` (or `nprobe`) and record recall@k and p50/p95 latency at realistic concurrency.
4. Pick the cheapest setting that meets the target (for example recall@20 at least 0.95), and repeat after data doubles or the embedding model changes.

### pgvector specifics

pgvector is covered in depth in [pgvector]({{ '/postgresql/pgvector/' | relative_url }}); the parts that matter for ANN:

```sql
-- Build: let the graph fit in memory, use parallel workers.
SET maintenance_work_mem = '8GB';
SET max_parallel_maintenance_workers = 7;
CREATE INDEX CONCURRENTLY chunk_emb_hnsw
    ON chunk_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Query: ORDER BY distance LIMIT k is the shape the planner can index.
BEGIN;
SET LOCAL hnsw.ef_search = 100;
SET LOCAL hnsw.iterative_scan = relaxed_order;   -- pgvector 0.8+
SELECT chunk_id, embedding <=> $1 AS distance
FROM chunk_embeddings
WHERE tenant_id = $2
ORDER BY embedding <=> $1
LIMIT 20;
COMMIT;
```

- Operators: `<->` L2, `<#>` negative inner product, `<=>` cosine distance, `<~>` Hamming. The opclass (`vector_l2_ops`, `vector_ip_ops`, `vector_cosine_ops`, `bit_hamming_ops`) must match the operator in `ORDER BY`, or the index is not used.
- Defaults: `m = 16`, `ef_construction = 64`, `hnsw.ef_search = 40`, `ivfflat.probes = 1`. Without iterative scan, an HNSW scan returns at most `ef_search` rows, so a `LIMIT 100` with the default silently returns fewer.
- Iterative scan (0.8+): `strict_order` or `relaxed_order`, bounded by `hnsw.max_scan_tuples` (default 20,000). With `relaxed_order`, wrap the query and re-sort by distance if exact order matters.
- Indexable dims: `vector` up to 2,000, `halfvec` up to 4,000, `bit` up to 64,000. For a 3,072-dim model, index a `halfvec` expression:

```sql
CREATE INDEX ON chunk_embeddings
    USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);

SELECT chunk_id FROM chunk_embeddings
ORDER BY embedding::halfvec(3072) <=> $1::halfvec(3072)
LIMIT 20;
```

- Binary quantization with rescoring, for a 1,024-dim column:

```sql
CREATE INDEX ON chunk_embeddings
    USING hnsw ((binary_quantize(embedding)::bit(1024)) bit_hamming_ops);

SELECT chunk_id FROM (
    SELECT chunk_id, embedding FROM chunk_embeddings
    ORDER BY binary_quantize(embedding)::bit(1024) <~> binary_quantize($1)
    LIMIT 200
) shortlist
ORDER BY embedding <=> $1
LIMIT 20;
```

- Exact search for a small tenant: a `MATERIALIZED` CTE stops the planner from using the global HNSW index and post-filtering.

```sql
WITH t AS MATERIALIZED (
    SELECT chunk_id, embedding FROM chunk_embeddings WHERE tenant_id = $2
)
SELECT chunk_id FROM t ORDER BY embedding <=> $1 LIMIT 20;
```

- Verify plans with `EXPLAIN (ANALYZE, BUFFERS)`: you want `Index Scan using chunk_emb_hnsw`, not `Seq Scan` plus `Sort`, unless exact was the intent.
- Vacuuming HNSW indexes after mass deletes is slow; the pgvector docs suggest reindexing first (`REINDEX INDEX CONCURRENTLY`) to speed it up.

### FAISS (illustrative)

FAISS is the standard library for in-process indexes and for benchmarking options before choosing an engine.

```python
import faiss
import numpy as np

d = 1024
xb = np.load("vectors.npy").astype("float32")   # (n, d)
xq = np.load("queries.npy").astype("float32")
faiss.normalize_L2(xb)
faiss.normalize_L2(xq)                          # L2 on unit vectors ranks like cosine

flat = faiss.IndexFlatIP(d)                     # exact baseline for recall
flat.add(xb)

hnsw = faiss.IndexHNSWFlat(d, 32)               # M = 32
hnsw.hnsw.efConstruction = 128
hnsw.add(xb)
hnsw.hnsw.efSearch = 64

ivfpq = faiss.index_factory(d, "IVF4096,PQ64", faiss.METRIC_INNER_PRODUCT)
ivfpq.train(xb[:300_000])                       # k-means wants tens of points per list
ivfpq.add(xb)
ivfpq.nprobe = 32

_, truth = flat.search(xq, 10)
_, approx = hnsw.search(xq, 10)
recall = np.mean([len(set(a) & set(t)) / 10 for a, t in zip(approx, truth)])
```

### Memory math

10M vectors at 1,024 dims:

| Layout | Vectors | Index extras | Rough RAM to stay fast |
|---|---|---|---|
| HNSW, float32 | 41 GB | links ~1–3 GB (`M = 16`) | ~45 GB |
| HNSW over `halfvec` | 20 GB | ~1–3 GB | ~23 GB |
| HNSW over binary + rescore | 1.3 GB | ~1–3 GB | ~4 GB, plus disk reads to rescore |
| IVF-PQ (`PQ64`) + refine | 0.64 GB codes + ids | centroids | ~1 GB, full vectors on disk |

In pgvector, the table also stores the vectors, so disk usage is roughly table plus index. Compare `pg_relation_size('chunk_emb_hnsw')` with available RAM; once the index stops fitting, p99 latency jumps from milliseconds to hundreds of milliseconds.

### pgvector vs a dedicated vector database

| Choose pgvector when | Choose a dedicated engine when |
|---|---|
| Up to roughly tens of millions of vectors | Hundreds of millions to billions |
| Vectors must join with relational data, transactions, row-level security | Vector search is the primary workload |
| One system to back up, monitor and secure | You need built-in sharding and replication for vectors |
| Filters are tenant and ACL predicates you already index | Heavy, highly selective filtered search at high QPS |
| Deletes must cascade with source rows | You want managed quantization, tiered storage, hybrid search |

Dedicated options include Qdrant, Weaviate, Milvus, Pinecone, Vespa and Elasticsearch or OpenSearch; as of 2025–2026 most offer HNSW with quantization, filter-aware search, hybrid sparse-plus-dense queries and per-tenant namespaces. Starting with pgvector is rarely wrong for a SaaS product. Move when the index outgrows RAM on a reasonable instance, vector queries hurt OLTP latency, or filtered recall cannot be made acceptable.

## Pitfalls

- **Index does not fit in RAM.** Symptom: p99 jumps by 10–100x as data grows; buffer cache hit ratio drops. Fix: `halfvec`, fewer dimensions, binary plus rescore, or more memory.
- **Too few results with filters.** Symptom: small tenants get 2 results instead of 20. Fix: iterative scan, exact search for small subsets, per-tenant partitions.
- **`LIMIT` above `ef_search`.** Symptom: queries return exactly 40 rows. Fix: raise `ef_search` to at least the limit, or enable iterative scan.
- **Planner skips the index.** Distance used in a `WHERE` threshold without `ORDER BY ... LIMIT`, or operator and opclass mismatch. Fix: query shape, then `EXPLAIN`.
- **IVF on drifting data.** Recall decays silently as new content lands in old lists. Fix: periodic rebuild, or HNSW.
- **Tuning on latency alone.** Fix: measure recall against exact search on real queries every time parameters, data volume or the model change.
- **Benchmark mismatch.** Public ANN benchmarks use unfiltered queries on fixed datasets. Fix: benchmark your filters, your dimensions and your concurrency.

## Checklist

- Default to HNSW; use exact search for small filtered subsets.
- Tune `ef_search` from a measured recall-vs-latency curve, and keep it at least `LIMIT`.
- Match the distance operator to the opclass; verify with `EXPLAIN`.
- Plan filtering explicitly: iterative scan, partitions, or exact search per tenant size.
- Keep the index in RAM; compress with `halfvec`, int8 or binary plus rescore.
- Reach for IVF-PQ or DiskANN only at hundreds of millions of vectors.

## Interview questions

**HNSW vs IVF?** HNSW is a layered proximity graph: better recall at a given latency and incremental inserts, but more memory and slower builds. IVF clusters vectors and scans a few clusters: smaller and faster to build, but needs training and loses recall at cluster boundaries and under drift.

**How does HNSW search work?** Greedy descent through sparse upper layers to find a good entry point, then a beam search of width `ef_search` on the bottom layer, returning the best `k`.

**What does product quantization do?** It splits vectors into subvectors and replaces each with a 1-byte centroid id from a per-subspace codebook, compressing 4 KB to tens of bytes. Distances come from a precomputed query-to-centroid lookup table; full vectors rescore the top candidates.

**Why are filters hard for ANN?** The index finds nearest neighbors globally. Selective filters discard most candidates, so you either scan further (latency), return fewer results (recall), or need filter-aware traversal.

**When is brute force the right answer?** When the candidate set after filtering is small, roughly up to tens of thousands of vectors: exact, fast enough, and no index to tune.

**pgvector or a dedicated vector DB?** pgvector up to tens of millions of vectors when you need joins, transactions and one operational system. A dedicated engine at much larger scale, for heavy filtered search at high QPS, or when you need sharding and tiered storage.

## Further reading

- Malkov and Yashunin, "Efficient and Robust Approximate Nearest Neighbor Search Using Hierarchical Navigable Small World Graphs" (2016, journal version 2018).
- Jégou, Douze and Schmid, "Product Quantization for Nearest Neighbor Search" (2011).
- Subramanya et al., "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node" (2019).
- Johnson, Douze and Jégou, "Billion-scale similarity search with GPUs" (2017), the FAISS paper.
- Aumüller, Bernhardsson and Faithfull, "ANN-Benchmarks: A Benchmarking Tool for Approximate Nearest Neighbor Algorithms" (2018).
- pgvector README (official documentation on GitHub).
