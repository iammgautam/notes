---
title: Embeddings
section: llm
order: 13
phase: "Phase 3 · Retrieval"
summary: How embedding models are trained, dense vs sparse vs multi-vector, metrics, dimensions, quantization, model choice and safe re-embedding migrations.
---

An embedding model maps text (or images, or code) to a fixed-length vector so that inputs with similar meaning land close together. That one property powers semantic search, RAG retrieval, deduplication and clustering. Embedding calls are cheap, but the vectors become a data asset: once tens of millions of chunks are embedded, the model, dimension, normalization and preprocessing are baked into storage, indexes and query code, and changing any of them is a migration.

## How it works

### What a vector captures

| Captures well | Captures poorly |
|---|---|
| Topic, paraphrase, synonyms | Exact identifiers (SKUs, error codes, names) |
| Question-to-answer matching | Negation ("refund" vs "no refund") |
| Cross-lingual meaning (multilingual models) | Numbers, dates and ranges |
| Rough intent and domain | Recency, authority, permissions |

The right-hand column is why production retrieval adds keyword search, metadata filters and reranking on top of vectors (see [hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }})).

### Bi-encoders and pooling

Most embedding models are **bi-encoders**: a transformer encodes the query and the document independently. A pooling step collapses per-token hidden states into one vector: mean pooling over tokens, the `[CLS]` token's state (BERT-style models), or the last token's state for decoder-only LLMs adapted into embedders. Independence is the point: documents are embedded once offline; a query costs one forward pass plus an index lookup.

A **cross-encoder** instead reads query and document together and outputs one relevance score. It is far more accurate but needs a forward pass per pair and cannot build an index. Hence the standard split: retrieve with a bi-encoder, rerank the top 50–200 with a cross-encoder.

```text
bi-encoder (retrieval)              cross-encoder (reranking)

query --> encoder --> q --+         [query ; doc] --> encoder
doc   --> encoder --> d --+--> q.d                       |
                                                   relevance score
docs encoded once, offline          one pass per (query, doc) pair
one pass per query                  only feasible on a shortlist
```

### Training: contrastive learning

The standard objective is InfoNCE, a softmax cross-entropy that pushes a matching pair's similarity above the non-matching ones:

`loss = -log( exp(s(q, d+) / t) / sum_j exp(s(q, d_j) / t) )`

`s` is cosine similarity, `d+` is the positive document for query `q`, the sum runs over the positive and every negative, and `t` is a small temperature (commonly around 0.01–0.05) that sharpens the distribution.

- **In-batch negatives.** In a batch of `B` (query, positive) pairs, each query uses the other `B - 1` positives as negatives. The scores are one `B x B` matrix multiply with the diagonal as the correct class, so negatives cost nothing extra. More negatives give a better training signal, which is why embedding training uses batches in the thousands and memory tricks such as gradient caching. In sentence-transformers this is `MultipleNegativesRankingLoss` (and `CachedMultipleNegativesRankingLoss` for large batches).
- **Hard negatives.** Random in-batch negatives are mostly easy. Mining negatives that look relevant but are not (top BM25 hits, or top hits from an earlier model that are not labeled positive) teaches finer distinctions. The risk is false negatives: relevant but unlabeled passages that teach the model the wrong thing. Filtering mined negatives with a cross-encoder score is common.
- **Stages.** Typical recipes pretrain contrastively on hundreds of millions to billions of weakly paired texts mined from the web (title and body, question and answer, post and reply), then fine-tune on smaller labeled sets (search queries with judged passages, NLI pairs, task data), often with a task instruction prepended so one model serves retrieval, clustering and classification.

The training pairs define "similar": a model trained on paraphrase pairs is good at duplicate detection and mediocre at question-to-passage retrieval. The same mechanism applies when [fine-tuning embeddings]({{ '/llm/fine-tuning-embeddings/' | relative_url }}) on your own pairs.

### Dense, sparse and multi-vector

| Type | Representation | Scoring | Strengths | Costs |
|---|---|---|---|---|
| Dense | One vector, ~384–4,096 dims | cosine or dot | Paraphrase, semantics, cross-lingual | Weak on exact terms |
| BM25 | Term weights over vocabulary | term overlap | Exact match, no model, explainable | No synonyms |
| Learned sparse (SPLADE) | Vocab-size vector, mostly zeros | dot over shared terms | Exact match plus learned expansion | Needs a model; bigger postings |
| Multi-vector (ColBERT) | One small vector per token | late interaction | Best quality, robust out of domain | 10–100x storage, special index |

**SPLADE** runs a masked-language-model head over the input and converts each vocabulary term's logits into a non-negative weight (`log(1 + ReLU(logit))`, pooled over input tokens). A sparsity regularizer drives most weights to zero, leaving a few hundred terms, including terms that never appear in the text ("sedan" activating "car"). The result fits an ordinary inverted index, giving lexical precision plus some semantic recall. pgvector's `sparsevec` type and most vector databases can store it.

**ColBERT** keeps one vector per token (128 dims in the original) and scores with late interaction: for each query token, take its best match among the document tokens, then sum.

`score(q, d) = sum_i max_j (q_i . d_j)`

Matching per token handles rare terms and long passages better than one pooled vector. The price is storage: a 300-token chunk becomes 300 vectors (ColBERTv2 compresses each to a few dozen bytes), and it needs its own index type. A common compromise is to use late interaction only to rerank dense candidates. Some models emit several representations from one forward pass (BGE-M3 produces dense, sparse and multi-vector outputs), which makes hybrid retrieval cheaper to operate.

### Dimensions and Matryoshka truncation

Storage and distance cost grow linearly with dimension; quality does not. **Matryoshka representation learning** applies the training loss to several nested prefixes at once (for example the first 64, 128, 256, ... dims), so leading components carry the most information and any prefix is a usable embedding. To truncate, keep the first `n` components and **renormalize**. Models trained this way typically lose only a few points of retrieval quality at a quarter of the size; models not trained this way degrade badly when truncated. Some hosted APIs expose it directly (OpenAI's `text-embedding-3` models take a `dimensions` parameter), and sentence-transformers can truncate at load time.

A useful pattern is a funnel: find candidates with short prefixes or binary codes, then rescore them with full vectors.

### Normalization and similarity metrics

| Metric | Formula | Notes |
|---|---|---|
| Cosine | `a.b / (norm(a) * norm(b))` | Ignores magnitude; range -1..1 |
| Dot product | `a.b` | Equals cosine on unit vectors; cheapest |
| Euclidean (L2) | `norm(a - b)` | On unit vectors `norm(a - b)^2 = 2 - 2 cos(a, b)` |

On L2-normalized vectors all three produce the same ranking, so normalize once at write time and use inner product. Without normalization, dot product rewards vector length, which often tracks text length rather than relevance. A few models are trained for raw dot product, so use what the model card says. Absolute scores are not comparable across models: 0.8 is a strong match in one model and noise in another, so any similarity threshold must be calibrated per model.

### Asymmetric queries and documents

Queries are short and question-shaped; documents are long and declarative. Many models expect a marker for each side:

- **Prefixes on both sides**: E5-style `query: ...` and `passage: ...`.
- **Instruction on the query side only**: BGE and instruction-tuned embedders put a task description (for example "Represent this question for retrieving supporting documents:") before the query.
- **API parameters**: hosted APIs such as Cohere's and Voyage's take an input type (query vs document) instead of a text prefix.

Forgetting the prefix, using the document prefix for queries, or adding a retrieval prefix to a symmetric task (duplicate detection) silently costs recall. Treat the prefix scheme as part of the model id.

### Multilingual models

Multilingual models (multilingual E5, BGE-M3, most large hosted models) map languages into a shared space, so a Hindi query can retrieve an English passage. Caveats:

- Quality varies a lot by language; low-resource languages lag. Evaluate each language you serve.
- Same-language bias: a query tends to prefer passages in its own language even when a better answer exists in another.
- Tokenizers spend more tokens per word on many non-Latin scripts, so a chunk sized in characters hits the input limit sooner.

### Choosing a model

MTEB (Massive Text Embedding Benchmark) covers retrieval, reranking, classification, clustering and similarity tasks, with multilingual variants. Use its leaderboard for a shortlist, not a decision:

- Read the **retrieval** columns, not the overall average, which blends tasks you do not run.
- Benchmarks leak. Many models train on the training splits of benchmark datasets, and some are tuned toward the leaderboard. Gaps of a point or two rarely survive transfer to your domain.
- Public datasets are mostly web and academic text; your tickets, contracts and code look different.
- Check what it does not show: max input length, Matryoshka support, license, latency on your hardware, price, and how long the provider will keep the model available.

Then run your own eval: 100–300 real queries with labeled relevant chunks, comparing recall@k and nDCG@10 for three or four candidates on your actual chunking (see [RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }})). A model two points lower on MTEB that you can self-host at a tenth of the cost may be the right call.

## In practice

### Runnable: metrics, truncation, quantization

Stdlib only. Synthetic vectors stand in for model output, with more variance in the leading dims, roughly what Matryoshka training produces.

```python
import heapq
import math
import random

def dot(a, b):
    return sum(x * y for x, y in zip(a, b))

def norm(a):
    return math.sqrt(dot(a, a))

def normalize(a):
    n = norm(a)
    return [x / n for x in a]

def cosine(a, b):
    return dot(a, b) / (norm(a) * norm(b))

def l2(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

def truncate(a, dims):
    """Matryoshka truncation: keep the prefix, then renormalize."""
    return normalize(a[:dims])

def int8_scale(sample):
    """Global symmetric calibration: largest |component| maps to 127."""
    return 127 / max(abs(x) for v in sample for x in v)

def to_int8(a, scale):
    return [max(-127, min(127, round(x * scale))) for x in a]

def to_bits(a):
    return [1 if x > 0 else 0 for x in a]

def hamming(a, b):
    return sum(x != y for x, y in zip(a, b))

def top_k(query, vectors, k, score):
    return [i for _, i in heapq.nlargest(
        k, ((score(query, v), i) for i, v in enumerate(vectors)))]

def fake_corpus(n, dims, rng):
    decay = [1 / math.sqrt(1 + i / 16) for i in range(dims)]
    return [normalize([rng.gauss(0, 1) * w for w in decay]) for _ in range(n)]

if __name__ == "__main__":
    rng = random.Random(0)
    docs = fake_corpus(1000, 256, rng)
    target = 42
    query = normalize([x + rng.gauss(0, 0.03) for x in docs[target]])

    # 1. On unit vectors, cosine == dot and L2 gives the same ranking.
    a, b = docs[1], docs[2]
    assert abs(cosine(a, b) - dot(a, b)) < 1e-9
    assert abs(l2(a, b) ** 2 - (2 - 2 * dot(a, b))) < 1e-9
    by_dot = top_k(query, docs, 10, dot)
    by_l2 = top_k(query, docs, 10, lambda q, v: -l2(q, v))
    assert by_dot == by_l2 and by_dot[0] == target

    # 2. Without normalization, dot product rewards length.
    runner_up = by_dot[1]
    raw = docs[:]
    raw[runner_up] = [x * 10 for x in docs[runner_up]]  # same direction
    assert top_k(query, raw, 1, cosine) == [target]
    assert top_k(query, raw, 1, dot) == [runner_up]

    # 3. Truncation to 1/4 of the dims keeps the top hit.
    short = [truncate(v, 64) for v in docs]
    assert top_k(truncate(query, 64), short, 1, dot) == [target]

    # 4. int8: 4x smaller, near-identical top-10.
    scale = int8_scale(docs[:200])
    docs8 = [to_int8(v, scale) for v in docs]
    hits8 = top_k(to_int8(query, scale), docs8, 10, dot)
    overlap = len(set(hits8) & set(by_dot)) / 10
    assert overlap >= 0.8, overlap

    # 5. Binary: 32x smaller. Shortlist by Hamming, rescore with floats.
    #    Recall depends on how deep you rescore.
    qb, bits = to_bits(query), [to_bits(v) for v in docs]
    recalls = []
    for depth in (10, 50, 200):
        shortlist = top_k(qb, bits, depth, lambda q, v: -hamming(q, v))
        order = top_k(query, [docs[i] for i in shortlist], 10, dot)
        final = [shortlist[i] for i in order]
        assert final[0] == target
        recalls.append(len(set(final) & set(by_dot)) / 10)
    assert recalls == sorted(recalls) and recalls[-1] >= 0.8, recalls

    print("int8 top-10 overlap:", overlap)
    print("binary+rescore recall@10 at depth 10/50/200:", recalls)
```

The binary result is the lesson: the top hit survives, but recall@10 only recovers when you rescore a shortlist several times larger than `k` (0.1, 0.4, 0.8 here).

### Encoding with sentence-transformers (illustrative)

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("intfloat/e5-base-v2")
docs = ["passage: Refunds are issued within 30 days of purchase.",
        "passage: API keys can be rotated from the dashboard."]
query = "query: how long do refunds take"

doc_vecs = model.encode(docs, batch_size=64, normalize_embeddings=True)
q_vec = model.encode(query, normalize_embeddings=True)
scores = doc_vecs @ q_vec   # cosine, because both sides are unit length
```

### Storage math and quantized embeddings

10M chunks at 1,024 dims, before index overhead (graph links, list ids):

| Precision | Bytes per vector | 10M vectors | Typical role |
|---|---|---|---|
| float32 | 4,096 | ~41 GB | Source of truth, rescoring |
| float16 (`halfvec`) | 2,048 | ~20 GB | Default index storage |
| int8 scalar | 1,024 | ~10 GB | Index with small recall loss |
| Binary, 1 bit per dim | 128 | ~1.3 GB | First-pass candidates, then rescore |

- **int8 scalar quantization** maps each component to 256 levels using a range calibrated on a sample. 4x smaller, usually a small recall loss.
- **Binary quantization** keeps only the sign of each component; distance becomes Hamming distance (XOR plus popcount). 32x smaller. Quality loss depends on the model; higher-dimensional models tolerate it better. Always rescore: fetch 5–10x `k` candidates by Hamming distance, then rerank with float or int8 vectors.

Keep full-precision vectors even when the index is quantized; you need them to rescore and to rebuild without re-embedding. pgvector supports this pattern with `halfvec`, `bit` and `binary_quantize()` expression indexes (see [vector search]({{ '/llm/vector-search/' | relative_url }}) and [pgvector]({{ '/postgresql/pgvector/' | relative_url }})).

### Store provenance with every vector

```sql
CREATE TABLE chunk_embeddings (
    chunk_id      bigint       NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    tenant_id     bigint       NOT NULL,
    space         text         NOT NULL,  -- 'e5-base-v2@rev3/768/norm/e5-prefix'
    content_hash  bytea        NOT NULL,  -- sha256 of the exact embedded text
    embedding     vector(768)  NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, space)
);
```

Version the whole **embedding space**, not just the model name: model and revision, dimension after truncation, normalization, prefix scheme, and the preprocessing that built the input text. Vectors are comparable only if all of those match. Keying on `(chunk_id, space)` lets two spaces coexist during a migration; `content_hash` lets re-ingest skip unchanged text.

Embed documents in background workers in batches, with backoff on rate limits. Query embedding is on the hot path: cache it keyed by `(space, normalized query)`.

### Re-embedding migrations

Vectors from different models live in different spaces, even at the same dimension, so a model change invalidates every stored vector. The safe sequence:

```text
1. Add space B alongside A        rows keyed by (chunk_id, space)
2. Dual-write                     new or changed chunks get A and B
3. Backfill B                     throttled, resumable, by id range
4. Build B's index                CREATE INDEX CONCURRENTLY
5. Shadow-evaluate                same queries on A and B; compare
                                  recall@k and nDCG on a golden set
6. Flip a flag per tenant         query side switches to B
7. Retire A                       stop writes, drop rows and index
```

Worked numbers: 10M chunks at ~400 tokens is 4B tokens. At hosted prices on the order of $0.02–0.15 per million tokens (as of 2025–2026), that is roughly $80–600; money is rarely the constraint, throughput is. At a 1M tokens-per-minute rate limit, 4B tokens take about 67 hours. You also hold two copies of vectors and indexes during the transition, so check disk and RAM first.

> **Gotcha:** A config change that switches only the query-side model returns plausible but wrong results with no errors. Assert at query time that the query's space matches the index's space.

## Pitfalls

- **Mismatched spaces.** Symptom: retrieval quality collapses after a deploy, no errors. Fix: store the space id per row and check it at query time.
- **Missing or wrong prefixes.** Symptom: recall several points below what the model card promises. Fix: wrap encoding in one function per side that adds the prefix or input type.
- **Silent truncation.** Inputs past the model's max length lose their tail without an error. Fix: chunk below the limit using the model's own tokenizer to count.
- **Truncating without renormalizing**, or using dot product on unnormalized vectors. Symptom: long or generic chunks dominate results. Fix: normalize after every transformation.
- **Thresholds copied across models.** A "similarity > 0.8" filter tuned on one model returns nothing, or everything, on another. Fix: recalibrate per space on labeled pairs.
- **Embedding boilerplate.** Repeated headers, footers and navigation create near-duplicate vectors that crowd the top-k. Fix: clean and dedupe at ingest (see [document ingestion]({{ '/llm/document-ingestion/' | relative_url }})).
- **Quantizing without rescoring.** Fix: oversample candidates, rescore with full precision, measure recall against float32.

## Checklist

- One embedding space per index; space id stored with every vector.
- Normalize; use the model's metric, prefixes and tokenizer limit.
- Use Matryoshka truncation, `halfvec`, int8 or binary-plus-rescore before buying bigger machines.
- Keep full-precision vectors and content hashes.
- Choose models on your own retrieval eval; treat MTEB as a shortlist.
- Plan model changes as dual-write, backfill, shadow-eval, cutover.

## Interview questions

**How are embedding models trained?** Contrastively: an InfoNCE loss pushes a query's similarity to its positive above its similarity to negatives, which come from the batch and from hard-negative mining. Training goes from large weakly paired web data to smaller labeled sets.

**Why does batch size matter in contrastive training?** With in-batch negatives each query sees `B - 1` negatives, so larger batches give more and harder negatives. Gradient caching makes large batches fit in memory.

**Bi-encoder vs cross-encoder?** A bi-encoder embeds query and document separately, so documents can be indexed and search is fast. A cross-encoder reads them jointly, which is more accurate but costs one forward pass per pair, so it is used to rerank a shortlist.

**Dense vs SPLADE vs ColBERT?** Dense is one vector per text: good semantics, weak exact match. SPLADE is a learned sparse vector over the vocabulary with term expansion that fits an inverted index. ColBERT keeps per-token vectors and scores with MaxSim: best quality, much larger storage.

**What is Matryoshka representation learning?** Training the loss on nested prefixes so the first `n` dims are a usable embedding. You truncate and renormalize to trade a little quality for large storage and speed savings.

**How do you switch embedding models in production?** New space alongside the old, dual-write, backfill, build the index, shadow-evaluate on a golden set, flip per tenant, then drop the old space. Never mix spaces in one index.

**When would you use binary embeddings?** When memory or index size dominates. Search Hamming distance on 1-bit codes for a shortlist several times larger than `k`, then rescore with full-precision vectors.

## Further reading

- Reimers and Gurevych, "Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks" (2019).
- Karpukhin et al., "Dense Passage Retrieval for Open-Domain Question Answering" (2020).
- Khattab and Zaharia, "ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT" (2020).
- Formal et al., "SPLADE: Sparse Lexical and Expansion Model for First Stage Ranking" (2021).
- Kusupati et al., "Matryoshka Representation Learning" (2022).
- Muennighoff et al., "MTEB: Massive Text Embedding Benchmark" (2022).
