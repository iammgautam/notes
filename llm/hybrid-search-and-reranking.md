---
title: Hybrid search and reranking
section: llm
order: 18
phase: "Phase 3 · Retrieval"
summary: BM25 and dense retrieval, rank and score fusion, cross-encoder, LLM and late-interaction rerankers, and hybrid search in Postgres.
---

Pure vector search misses exact matches: error codes, SKUs, function names, people's names, acronyms the embedding model never saw. Pure keyword search misses paraphrase: "cancel my subscription" vs "terminate the plan". Hybrid search runs both and fuses the result lists; a reranker then reads each candidate next to the query and reorders them precisely. This two-stage shape (cheap, indexed recall, then expensive per-pair precision) is how search engines have worked for decades, and it is the most reliable upgrade to a basic RAG pipeline.

```text
                       query
                         │
            ┌────────────┴────────────┐
            ▼                         ▼
      BM25 / FTS                 dense ANN          stage 1: recall
      top 50                     top 50             indexed, ~10 ms
            └────────────┬────────────┘
                         ▼
                fuse (RRF) → 50–90 unique
                         │
                         ▼
             cross-encoder rerank                   stage 2: precision
             score every (query, passage)           cost ∝ candidates
                         │
                         ▼
             threshold, keep top 5–10 → prompt
```

## How it works

### BM25

BM25 scores a document by summing, over query terms, how rare the term is in the corpus times how often it appears in the document, with saturation and length normalization:

```text
score(D, Q) = Σ over t in Q of  IDF(t) * tf * (k1 + 1)
                                / (tf + k1 * (1 - b + b * |D| / avgdl))
IDF(t)      = ln(1 + (N - n_t + 0.5) / (n_t + 0.5))

tf     occurrences of t in D        N     documents in the corpus
|D|    length of D in tokens        n_t   documents containing t
avgdl  average document length      k1 ≈ 1.2–2.0, b ≈ 0.75
```

- **IDF** makes rare terms count: `ERR_CONN_RESET` in 3 of 100,000 chunks dominates `connection` in 20,000.
- **`k1` saturates term frequency.** The tenth occurrence adds little; keyword stuffing does not win.
- **`b` normalizes length.** A long document mentioning a term once is less about it than a short one. `b = 0` turns normalization off.

BM25 is the default ranker in Lucene, Elasticsearch and OpenSearch. PostgreSQL full-text search is related but different: `ts_rank` and `ts_rank_cd` use term frequency and proximity within the document but no corpus-wide statistics, so there is no IDF. For hybrid retrieval that is usually acceptable because fusion uses ranks, not scores. If you need real BM25 inside Postgres, extensions such as ParadeDB's `pg_search` provide it. See [Full-text search]({{ '/postgresql/full-text-search/' | relative_url }}).

### Sparse vs dense

| | Sparse lexical (BM25, FTS) | Dense (bi-encoder embeddings) | Learned sparse (SPLADE-style) |
|---|---|---|---|
| Representation | Term counts, inverted index | One vector per chunk, ANN index | Weighted terms incl. expansions, inverted index |
| Strong at | Identifiers, names, rare terms, exact phrases | Paraphrase, synonyms, intent, cross-lingual | Both, partly; expansion adds synonyms |
| Weak at | Synonyms, typos, vocabulary mismatch | Exact tokens, numbers, negation, new jargon | Needs a trained model; larger index |
| Training needed | None | Embedding model; re-embed on change | Model; re-encode on change |
| Explainability | High (which terms matched) | Low | Medium |

Neither dominates across query types; on mixed real-world traffic, the union recalls more than either alone. That is the whole argument for hybrid. See [Embeddings]({{ '/llm/embeddings/' | relative_url }}) and [Vector search]({{ '/llm/vector-search/' | relative_url }}) for the dense side.

### Fusion

You now have two ranked lists with incompatible scores: cosine similarity around 0.3–0.9, `ts_rank_cd` around 0.001–0.1, BM25 unbounded. Adding them raw lets the larger scale win silently.

**Reciprocal rank fusion (RRF)** ignores scores and uses ranks (Cormack et al. 2009):

```text
RRF(d) = Σ over lists r of  w_r / (k + rank_r(d))     k = 60 by convention
```

A document ranked 1st in one list and absent from the other scores `1/61 ≈ 0.0164`. Ranked 3rd in both, it scores `2/63 ≈ 0.0317`. Agreement wins. `k` flattens the advantage of top ranks; 60 works well enough that tuning it rarely pays. Per-list weights `w_r` let you trust one retriever more once your eval says so.

**Score fusion** normalizes each list per query, then takes a weighted sum: `alpha * dense_norm + (1 - alpha) * sparse_norm`. Min-max normalization maps each list to `[0, 1]`; z-score normalization uses mean and standard deviation. It uses more information than RRF (a huge score gap means something) and can beat it when `alpha` is tuned on your golden set, but it is fragile: min-max always maps the best result to 1 even when every result is bad, and score distributions shift across queries and model versions.

| | RRF | Normalized score fusion |
|---|---|---|
| Needs tuning | Almost none | `alpha` and normalization choice |
| Uses score magnitude | No | Yes |
| Robust to model changes | Yes | Re-tune after changes |
| Default | Start here | Try when you have an eval set |

### BM25 and fusion from scratch

```python
import math
import re
from collections import Counter


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.tf = [Counter(tokenize(d)) for d in docs]
        self.len = [sum(tf.values()) for tf in self.tf]
        self.avgdl = sum(self.len) / len(docs)
        self.k1, self.b, n = k1, b, len(docs)
        df = Counter(t for tf in self.tf for t in tf)
        # Lucene-style IDF: always positive, even for very common terms.
        self.idf = {t: math.log(1 + (n - m + 0.5) / (m + 0.5))
                    for t, m in df.items()}

    def score(self, query: str, i: int) -> float:
        tf, norm = self.tf[i], 1 - self.b + self.b * self.len[i] / self.avgdl
        return sum(self.idf[t] * tf[t] * (self.k1 + 1)
                   / (tf[t] + self.k1 * norm)
                   for t in tokenize(query) if tf[t])

    def search(self, query: str, k: int = 10) -> list[int]:
        scored = sorted(((self.score(query, i), i)
                         for i in range(len(self.tf))), reverse=True)
        return [i for s, i in scored[:k] if s > 0]


def rrf(rankings: list[list], k: int = 60, weights=None) -> list:
    weights = weights or [1.0] * len(rankings)
    fused = Counter()
    for w, ranking in zip(weights, rankings):
        for rank, doc in enumerate(ranking, start=1):
            fused[doc] += w / (k + rank)
    return [d for d, _ in fused.most_common()]


def minmax(scores: dict) -> dict:
    lo, hi = min(scores.values()), max(scores.values())
    return {d: (s - lo) / (hi - lo) if hi > lo else 1.0
            for d, s in scores.items()}


def weighted_fusion(score_maps: list[dict], weights: list[float]) -> list:
    fused = Counter()
    for w, scores in zip(weights, score_maps):
        for d, s in minmax(scores).items():  # missing doc contributes 0
            fused[d] += w * s
    return [d for d, _ in fused.most_common()]


if __name__ == "__main__":
    docs = [
        "Upgrade guide: connection resets after upgrading to v2.3",
        "ERR_CONN_RESET is raised when the proxy drops idle sockets",
        "How to cancel your subscription and request a refund",
        "Network errors and retries: the client retries idempotent calls",
        "the the the the the the the the the the connection",
    ]
    bm = BM25(docs)
    assert bm.search("what causes ERR_CONN_RESET") == [1]  # exact token
    assert bm.idf["err_conn_reset"] > bm.idf["the"]
    # Saturation: ten repeats of a term are worth far less than ten times one.
    one = BM25(["cat dog", "dog bird"]).score("cat", 0)
    ten = BM25(["cat " * 10 + "dog", "dog bird"]).score("cat", 0)
    assert one < ten < 3 * one

    # RRF: agreement beats a single first place.
    vec, kw = ["a", "b", "c"], ["d", "e", "c"]
    assert rrf([vec, ["c", "x", "y"]])[0] == "c"
    assert rrf([vec, kw])[0] == "c"  # 3rd in both > 1st in one
    weighted = rrf([vec, kw], weights=[1.0, 3.0])  # trust keyword list more
    assert weighted.index("d") < weighted.index("a")

    # Raw score sums let the larger-scale retriever dominate.
    cosine = {"a": 0.62, "b": 0.60, "c": 0.58, "d": 0.40}
    ts_rank = {"c": 0.03, "b": 0.01, "a": 0.002}
    raw = Counter(cosine)
    raw.update(ts_rank)
    assert raw.most_common(1)[0][0] == "a"  # FTS effectively ignored
    assert weighted_fusion([cosine, ts_rank], [0.5, 0.5])[0] == "c"
    print("ok")
```

## Rerankers

### Bi-encoders vs cross-encoders

| | Bi-encoder (embeddings) | Cross-encoder (reranker) |
|---|---|---|
| Input | Query and document encoded separately | Query and document in one sequence |
| Precompute | Document vectors stored ahead of time | Nothing; runs per query-document pair |
| Cost per query | One embedding plus an index lookup | One forward pass per candidate |
| Quality | Good recall, coarse ranking | Much better precision |
| Scale | Millions to billions of documents | Tens to low hundreds of candidates |

A bi-encoder compresses a passage into one vector before it has seen the question, so it cannot tell "refunds are available within 30 days" from "refunds are not available after 30 days" very well. A cross-encoder feeds `[CLS] query [SEP] passage` through a transformer and reads a relevance score off the top, so every query token attends to every passage token. It handles negation, specificity and multi-part questions far better. The trade: nothing can be precomputed, so it only runs on a short candidate list. Nogueira and Cho (2019) showed that a BERT cross-encoder reranking BM25 candidates improved MS MARCO passage ranking by a wide margin; the pattern has been standard since.

Options as of 2025–2026: open cross-encoders (small MiniLM-class models for CPU, larger multilingual rerankers for GPU), hosted rerank APIs from several embedding vendors, and LLMs used as rerankers.

### LLM rerankers

| Style | How | Cost | Notes |
|---|---|---|---|
| Pointwise | Score each passage alone (0–10, or the probability of "yes, relevant") | n calls, parallel | Simple; scores poorly calibrated across queries |
| Pairwise | "Which of A and B is more relevant?" | Up to n² comparisons | Accurate, expensive; rarely worth it online |
| Listwise | Show many passages, ask for a ranked permutation of ids | Few large calls | Best quality per call; position bias, parse failures |

Listwise reranking with a sliding window (RankGPT, Sun et al. 2023) ranks the last window of 20 candidates, slides toward the top by 10, and repeats, so strong passages bubble up. LLM rerankers are the most flexible (you can add instructions like "prefer the newest policy") and the slowest. Use them offline for labeling, for low-QPS high-value queries, or distilled into a cross-encoder.

### Late interaction: ColBERT

ColBERT (Khattab and Zaharia 2020) sits between the two: it stores one vector per document token, encodes the query into per-token vectors, and scores with MaxSim, where each query token takes its best-matching document token:

```text
score(q, d) = Σ over query tokens i of  max over doc tokens j of  q_i · d_j
```

Documents are still precomputed, so it can serve as a first-stage retriever or a reranker, with quality close to a cross-encoder. The cost is storage: one vector per token instead of per chunk, which ColBERTv2 (Santhanam et al. 2022) attacks with residual compression.

```python
def maxsim(query_vecs: list[list[float]], doc_vecs: list[list[float]]) -> float:
    """ColBERT late interaction: each query token takes its best doc token."""
    dot = lambda u, v: sum(a * b for a, b in zip(u, v))
    return sum(max(dot(q, d) for d in doc_vecs) for q in query_vecs)


if __name__ == "__main__":
    # Toy 2-d token vectors: axis 0 ~ "refund", axis 1 ~ "annual plan".
    query = [[1.0, 0.0], [0.0, 1.0]]
    covers_both = [[0.9, 0.1], [0.1, 0.9], [0.0, 0.0]]
    one_topic_repeated = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    assert maxsim(query, covers_both) > maxsim(query, one_topic_repeated)
    assert maxsim(query, covers_both) == 0.9 + 0.9
    print("ok")
```

### Cost and latency of reranking

Cross-encoder cost is linear in candidates times sequence length. Reranking 50 candidates of ~300 tokens with a ~30-token query pushes about `50 * 330 ≈ 16,500` tokens through the model per query.

| Reranker | 50 candidates, order of magnitude |
|---|---|
| Small cross-encoder, GPU, one batch | 10–50 ms |
| Small cross-encoder, CPU | 100–500 ms |
| Large cross-encoder or hosted API | 100–300 ms |
| LLM listwise, sliding window | 1–5 s, several sequential calls |

LLM cost math, with an illustrative price of $1 per million input tokens: 50 passages × 300 tokens ≈ 15,000 tokens ≈ $0.015 per query, about $1,500 per day at 100,000 queries. A self-hosted cross-encoder on one GPU handles that volume for a fraction of the cost.

Pick the candidate count from data: plot first-stage recall@N on your golden set. Where the curve flattens (often 30–100) is your rerank depth; beyond it you pay linear latency for nothing.

## In practice

### Hybrid search in Postgres

```sql
ALTER TABLE chunks
    ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX chunks_emb_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops);

-- $1 query embedding, $2 raw query text, $3 tenant_id
BEGIN;
SET LOCAL hnsw.ef_search = 100;
SET LOCAL hnsw.iterative_scan = relaxed_order;   -- pgvector 0.8+
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> $1) AS rnk
    FROM chunks
    WHERE tenant_id = $3
    ORDER BY embedding <=> $1
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
       coalesce(1.0 / (60 + vec.rnk), 0)
     + coalesce(1.0 / (60 + fts.rnk), 0) AS rrf
FROM vec FULL OUTER JOIN fts USING (id)
ORDER BY rrf DESC
LIMIT 50;
COMMIT;
```

The `FULL OUTER JOIN` keeps documents found by only one retriever. Each branch is tenant-filtered and uses its own index. `websearch_to_tsquery` safely parses user input (quotes, `or`, `-term`). For code-like tokens that stemming mangles, add a column with the `simple` configuration or a `pg_trgm` index. Weight a branch by multiplying its term, e.g. `1.5 / (60 + fts.rnk)`.

### Reranking stage

Illustrative, using sentence-transformers' `CrossEncoder`:

```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512)
RERANK_MIN_SCORE = 0.0  # calibrate on your golden set; scale is model-specific


def retrieve(query: str, tenant_id: int, k_final: int = 8) -> list:
    candidates = hybrid_search(query, tenant_id, limit=50)  # the SQL above
    if not candidates:
        return []
    scores = reranker.predict([(query, c.text) for c in candidates],
                              batch_size=64)
    ranked = sorted(zip(candidates, scores), key=lambda p: p[1], reverse=True)
    # Return fewer passages, or none, instead of always filling k slots.
    return [c for c, s in ranked[:k_final] if s >= RERANK_MIN_SCORE]
```

The threshold feeds the refusal path in [RAG architecture]({{ '/llm/rag-architecture/' | relative_url }}). Reranker scores are logits or probabilities depending on the model; recalibrate whenever you swap models.

### When to add each piece

| Symptom | Add |
|---|---|
| Exact identifiers, names or codes not found | Full-text branch; trigram or `simple` config for code tokens |
| Right passage in top 50, not in top 5 | Cross-encoder reranker |
| Right passage not in top 50 at all | Fix chunking, embeddings or the query; reranking cannot help |
| Many near-duplicates in the final set | Dedupe or MMR diversity after reranking |
| Ranking needs instructions (recency, authority) | LLM reranker or a metadata boost after reranking |

## Pitfalls

- **Adding raw scores.** Cosine plus `ts_rank` silently ignores FTS. Fix: RRF or per-query normalization; the demo shows the failure.
- **Reranker truncation.** Many cross-encoders cap input around 512 tokens, so long passages are scored on their beginning. Fix: rerank chunk-sized text, expand to parents afterward.
- **Language config mismatch.** English stemming on German content matches poorly. Fix: per-document language column and matching `regconfig`, or `simple`.
- **Reranking too many candidates.** Symptom: p95 dominated by rerank. Fix: cap from the recall curve, batch, use GPU or a smaller model.
- **LLM reranker position bias.** Order in the prompt changes the ranking. Fix: sliding windows, shuffle tests, or distill into a cross-encoder.
- **No per-stage evaluation.** Fix: recall@50 for stage one, recall@5 and MRR after rerank, measured separately; see [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }}).

## Checklist

- Run vector and full-text retrieval in parallel, both tenant-filtered.
- Fuse with RRF (`k = 60`) before tuning anything fancier.
- Rerank 30–100 candidates to 5–10 with a cross-encoder; choose depth from the recall curve.
- Threshold rerank scores so "no relevant passage" is possible.
- Recalibrate thresholds and fusion weights when any model changes.

## Interview questions

**Why hybrid search?** Dense retrieval misses exact tokens and rare terms; lexical retrieval misses paraphrase. Their errors are different, so the fused list recalls more than either.

**Explain BM25.** Sum over query terms of IDF times saturated term frequency with length normalization; `k1` controls saturation, `b` controls length normalization.

**Why RRF instead of summing scores?** Scores from different retrievers are on different scales and shift per query; ranks are comparable. RRF needs no tuning and rewards agreement.

**Cross-encoder vs bi-encoder?** Bi-encoders embed query and document separately, so documents are precomputed and indexed. Cross-encoders read the pair jointly: far more precise, but one forward pass per candidate, so only for reranking a short list.

**Where does ColBERT fit?** Per-token embeddings with MaxSim scoring: precomputable like a bi-encoder, precise like a cross-encoder, at the cost of much larger storage.

**How many candidates should you rerank?** Enough to capture the recall plateau of the first stage, usually 30–100; beyond that, latency grows linearly with no gain.

## Further reading

- Robertson and Zaragoza, "The Probabilistic Relevance Framework: BM25 and Beyond" (2009).
- Cormack, Clarke and Büttcher, "Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods" (2009).
- Nogueira and Cho, "Passage Re-ranking with BERT" (2019).
- Khattab and Zaharia, "ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT" (2020).
- Formal et al., "SPLADE: Sparse Lexical and Expansion Model for First Stage Ranking" (2021).
- Sun et al., "Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents" (2023).
