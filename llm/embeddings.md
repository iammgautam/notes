---
title: Embeddings
section: llm
order: 3
summary: What embedding vectors capture, how to pick dimensions and similarity metrics, and how to migrate to a new embedding model without downtime.
---

An embedding model maps text (or images, or code) to a fixed-length vector so that semantically similar inputs land near each other. It is the foundation of semantic search, RAG retrieval, deduplication, clustering and classification. Embeddings are cheap per call, but they become a data asset: once millions of rows are embedded with one model, changing that model is a migration.

## How it works

### What a vector captures

An embedding model is usually a transformer encoder trained with contrastive learning: pull matching pairs (query and relevant passage, question and answer, paraphrases) together, push non-matching pairs apart. The result captures **topic and meaning** well and some things poorly:

| Captures well | Captures poorly |
|---|---|
| Topic, paraphrase, synonyms | Exact identifiers (SKUs, error codes, names) |
| Cross-lingual meaning (multilingual models) | Negation ("refund" vs "no refund") |
| Question-to-answer matching | Numbers and ranges |
| Rough intent | Recency, authority, permissions |

That right-hand column is why production retrieval adds keyword search, filters and reranking (see [hybrid search]({{ '/llm/hybrid-search-and-reranking/' | relative_url }})).

### Dimensions

Common sizes range from 384 to about 3,072 dimensions. More dimensions can hold more nuance but cost storage, memory and distance-computation time linearly.

```text
storage per vector = dims × bytes per component
1,536 dims × 4 bytes (float32) = 6 KB
10M chunks × 6 KB             = ~60 GB raw, before index overhead
same at float16 (halfvec)     = ~30 GB
```

Many modern models are trained with **Matryoshka representation learning**: the first `n` components are themselves a usable embedding. You can truncate a 3,072-dim vector to 512 or 1,024, renormalize, and lose only a little recall. This is the cheapest lever on storage and index size; measure it on your own eval set.

### Normalization and similarity

| Metric | Formula | Notes |
|---|---|---|
| Cosine similarity | `a·b / (‖a‖‖b‖)` | Ignores magnitude |
| Dot product | `a·b` | Equals cosine when vectors are unit length |
| Euclidean (L2) | `‖a − b‖` | On unit vectors, ranking matches cosine |

If vectors are L2-normalized, cosine, dot product and L2 produce the same ranking, and dot product is the cheapest to compute. Many APIs return normalized vectors already; normalize yourself anyway if you truncate dimensions or are unsure. Use the metric the model was trained for, and build the index with the matching operator class.

### Asymmetric retrieval

Queries are short; documents are long. Many models expect a different instruction or prefix for each side (for example `query:` and `passage:`, or an `input_type` parameter). Embedding queries as documents, or omitting the expected prefix, silently costs recall.

### Choosing a model

Do not pick from a leaderboard alone. Public benchmarks are averaged over many tasks and some models are tuned toward them. Decide on:

- **Your domain and languages.** Run your own retrieval eval with 50–200 real queries.
- **Max input length.** Inputs past the limit are truncated, often silently.
- **Dimensions and Matryoshka support**, for storage cost.
- **Hosted vs self-hosted.** Self-hosting a small open model on a CPU or single GPU is viable for high volume and data-residency needs; hosted APIs remove operational work.
- **Stability.** A provider deprecating a model forces a full re-embed.

## In practice

### Store provenance with every vector

```sql
CREATE TABLE chunk_embeddings (
    chunk_id      bigint      NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    tenant_id     bigint      NOT NULL,
    model         text        NOT NULL,        -- e.g. 'embed-v3@1024'
    content_hash  bytea       NOT NULL,        -- sha256 of the embedded text
    embedding     vector(1024) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model)
);
```

Keying on `(chunk_id, model)` lets two models coexist during a migration. `content_hash` lets you skip re-embedding unchanged text on re-ingest.

### Batch, normalize, cache

```python
import hashlib
import numpy as np

def embed_chunks(client, model: str, texts: list[str], batch_size: int = 128):
    out = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        vecs = np.asarray(client.embed(model=model, inputs=batch,
                                       input_type="document"), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)   # unit length
        out.extend(vecs)
    return out

def content_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()
```

Run embedding in background workers (Celery or a Postgres-backed queue), batch requests to cut per-call overhead, and respect provider rate limits with backoff. Query-time embedding is on the hot path: cache embeddings for repeated queries keyed by `(model, normalized_query)`.

### Re-embedding migration

Changing models means every stored vector is incompatible with new query vectors. Vectors from different models live in different spaces, even at the same dimension. The safe sequence:

```text
1. Add model B alongside A        (new rows keyed by model)
2. Dual-write: new/changed chunks embedded with A and B
3. Backfill B in background       (throttled, resumable, by id ranges)
4. Build B's index                (CREATE INDEX CONCURRENTLY)
5. Shadow-evaluate: run queries on both, compare recall@k on golden set
6. Flip a per-tenant flag to query B
7. Stop writing A, drop A's rows and index
```

Budget for it: re-embedding 10M chunks at a few hundred tokens each is billions of tokens and hours to days of throttled backfill. During the transition you also store two copies of vectors and two indexes.

> **Gotcha:** Query and document vectors must come from the same model and version. A config change that switches only the query-side model returns plausible-looking but wrong results, with no errors.

## Pitfalls

- **Mismatched query and document models.** Symptom: retrieval quality collapses after a deploy, no exceptions. Fix: store the model id with vectors and assert it matches at query time.
- **Silent truncation.** Chunks longer than the model's max input lose their tail. Fix: chunk below the limit and count tokens.
- **Wrong operator class.** An index built with L2 ops but queried with cosine distance is not used, or ranks differently. Fix: match the index ops to the query operator.
- **Unnormalized vectors with dot product.** Long documents with larger norms dominate results. Fix: normalize.
- **Embedding boilerplate.** Headers, footers and navigation repeated across pages create near-duplicate vectors that crowd results. Fix: clean text before embedding; dedupe by hash.
- **Leaderboard-driven choice.** Fix: evaluate on your own queries before committing.

## Checklist

- One embedding model per index; model id stored per row.
- Normalize vectors; use the metric the model was trained for.
- Use the model's query/document input types or prefixes.
- Consider Matryoshka truncation or `halfvec` for storage.
- Hash content to skip unchanged chunks on re-ingest.
- Plan re-embedding as a dual-write, backfill, shadow-eval, cutover migration.

## Interview angle

- **Cosine vs dot product?** Identical ranking on unit vectors; dot product is cheaper. Without normalization, dot product rewards magnitude.
- **Why not compare vectors across models?** Each model defines its own space; distances between spaces are meaningless.
- **How do you reduce vector storage?** Fewer dimensions (Matryoshka truncation), half-precision or quantized storage, and deduplication.
