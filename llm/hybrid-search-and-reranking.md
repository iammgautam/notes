---
title: Hybrid search and reranking
section: llm
order: 7
summary: Combine keyword and vector search with reciprocal rank fusion, then rerank with a cross-encoder to get the right passages into the prompt.
---

Pure vector search misses exact matches: error codes, SKUs, function names, people's names, acronyms the embedding model never saw. Pure keyword search misses paraphrases: "cancel my subscription" vs "terminate the plan". Hybrid search runs both and fuses the results; a reranker then reads each candidate alongside the query and reorders them precisely. This two-stage shape (cheap recall, expensive precision) is standard in search engines and is the most reliable upgrade to a basic RAG pipeline.

## How it works

### Lexical search: BM25 and Postgres full-text

BM25 scores documents by term frequency (with saturation, so the tenth occurrence adds little), inverse document frequency (rare terms matter more) and length normalization. It is what search engines like Elasticsearch, OpenSearch and Tantivy-based systems use by default.

PostgreSQL full-text search is close but not identical:

- `to_tsvector` stems and removes stop words per language config.
- `websearch_to_tsquery` parses user input safely (quotes, `or`, `-term`).
- `ts_rank` and `ts_rank_cd` rank by term frequency and proximity but lack BM25's corpus-wide IDF. Ranking is weaker on large heterogeneous corpora; for hybrid retrieval it is usually good enough because fusion only uses rank order. Extensions exist that add true BM25 to Postgres if you need it.
- Stemming hurts identifiers. `ERR_CONN_RESET` or `v2.3.1` may be tokenized unexpectedly; a `simple` config column or a trigram index (`pg_trgm`) helps for code-like tokens.

### Fusion: reciprocal rank fusion

Vector distances and text-rank scores are on incompatible scales, so adding them is meaningless without careful normalization. Reciprocal rank fusion (RRF) ignores scores and uses ranks:

```text
RRF(d) = Σ over result lists r of  1 / (k + rank_r(d))      with k ≈ 60
```

A document ranked 1st in one list and absent in the other scores `1/61 ≈ 0.0164`. A document ranked 3rd in both scores `2/63 ≈ 0.0317`. Agreement between retrievers wins, which is the behavior you want. `k` dampens the advantage of top ranks; 60 is the conventional default and rarely worth tuning. You can weight lists (`w_vec / (k + rank)`) if one retriever is consistently better on your eval.

The alternative is a weighted sum of normalized scores (min-max per query). It can beat RRF when tuned, but it is sensitive to score distributions that shift across queries. Start with RRF.

### Rerankers: bi-encoder vs cross-encoder

| | Bi-encoder (embeddings) | Cross-encoder (reranker) |
|---|---|---|
| Input | Query and document encoded separately | Query and document encoded together |
| Precompute | Document vectors stored ahead of time | Nothing; runs per query-document pair |
| Cost per query | One embedding + index lookup | One model forward pass per candidate |
| Accuracy | Good recall | Much better precision; sees word-level interactions |
| Scale | Millions of documents | Tens to low hundreds of candidates |

A cross-encoder reads "Can I get a refund after 45 days?" next to each passage and outputs a relevance score. It handles negation, specificity and multi-part questions far better than vector similarity. Options include hosted rerank APIs, open cross-encoder models served on CPU or GPU, and using an LLM as a listwise reranker (most accurate, slowest and most expensive).

Latency order of magnitude: reranking 50 passages of ~300 tokens with a small cross-encoder on a GPU takes tens of milliseconds; on CPU or via an API, roughly 100–300 ms. Budget it.

## In practice

### Schema for both retrievers

```sql
ALTER TABLE chunks
    ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;

CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_tenant ON chunks (tenant_id);
```

### Hybrid retrieval with RRF in one query

```sql
-- $1 query embedding, $2 raw query text, $3 tenant_id
WITH vec AS (
    SELECT c.id, row_number() OVER (ORDER BY e.embedding <=> $1) AS rnk
    FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id
    WHERE e.tenant_id = $3
    ORDER BY e.embedding <=> $1
    LIMIT 50
),
fts AS (
    SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rnk
    FROM chunks, websearch_to_tsquery('english', $2) AS q
    WHERE tenant_id = $3 AND tsv @@ q
    ORDER BY ts_rank_cd(tsv, q) DESC
    LIMIT 50
)
SELECT id,
       coalesce(1.0 / (60 + vec.rnk), 0) + coalesce(1.0 / (60 + fts.rnk), 0) AS rrf
FROM vec FULL OUTER JOIN fts USING (id)
ORDER BY rrf DESC
LIMIT 40;
```

The `FULL OUTER JOIN` keeps documents found by only one retriever. Each branch is independently indexed and tenant-filtered.

### Reranking stage

```python
def retrieve(query: str, tenant_id: int, k_final: int = 8) -> list[Hit]:
    q_emb = embed_query(query)
    candidates = hybrid_search(q_emb, query, tenant_id, limit=40)   # SQL above
    if not candidates:
        return []
    scores = reranker.score(query, [c.text for c in candidates])     # one batch
    ranked = sorted(zip(candidates, scores), key=lambda p: p[1], reverse=True)
    # Drop clearly irrelevant passages instead of always filling k slots.
    return [c for c, s in ranked[:k_final] if s >= RERANK_MIN_SCORE]
```

Calibrate `RERANK_MIN_SCORE` on your golden set. A threshold lets you return fewer passages, or none, which feeds the "I don't know" path instead of stuffing irrelevant context.

> **Rule of thumb:** Retrieve wide (30–100 candidates), rerank narrow (5–10). Recall is the job of the first stage, precision the job of the second.

### When to add each piece

| Symptom | Add |
|---|---|
| Exact identifiers, names or codes not found | Full-text branch (and trigram for code-like tokens) |
| Right passage in top 50 but not in top 5 | Cross-encoder reranker |
| Right passage not in top 50 at all | Fix chunking, embeddings, query rewriting; reranking cannot help |
| Good passages, but too many near-duplicates | Dedupe or diversity (MMR) after reranking |

## Pitfalls

- **Adding raw scores.** Cosine similarity in `[0, 1]` plus `ts_rank` in `[0, 0.1]` silently ignores FTS. Fix: RRF or per-query normalization.
- **Language config mismatch.** English stemming on German or multilingual content produces poor matches. Fix: per-document language column and matching `regconfig`, or `simple`.
- **Reranking too many candidates.** Latency grows linearly. Symptom: p95 dominated by rerank. Fix: cap candidates, batch, run the reranker on GPU or use a smaller model.
- **Reranker input truncation.** Cross-encoders have a max length (often 512 tokens). Long passages are cut and scored on their beginning only. Fix: rerank chunk-sized text, not parents.
- **No evaluation per stage.** Fix: measure recall@50 of the first stage and recall@5 / MRR after reranking separately.

## Checklist

- Run vector and full-text retrieval in parallel, both tenant-filtered.
- Fuse with RRF (`k = 60`) before tuning anything fancier.
- Rerank 30–100 candidates down to 5–10 with a cross-encoder.
- Use a score threshold to allow "no relevant passages".
- Evaluate each stage separately on a golden set.

## Interview angle

- **Why hybrid?** Embeddings miss exact tokens and rare terms; keyword search misses paraphrase. Fusion covers both with little extra cost.
- **Why RRF instead of summing scores?** Scores from different retrievers are not comparable; ranks are. RRF is robust without tuning.
- **Why not use a cross-encoder for everything?** It scores each query-document pair from scratch, so it cannot use a precomputed index. It is only affordable on a short candidate list.
