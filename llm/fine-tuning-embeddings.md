---
title: Fine-tuning embeddings and rerankers
section: llm
order: 38
phase: "Phase 6 · Fine-tuning"
summary: When to fine-tune retrieval models, how to build pairs and mine hard negatives, how contrastive losses work, and how to prove recall improved.
---

In a RAG system, the generator can only use what retrieval hands it. When retrieval misses, the answer is wrong no matter how good the LLM is. Off-the-shelf embedding models are trained on web-scale general data, so they understand "refund policy" but not that `PRJ-4471` and "the Atlas migration" are the same thing in your company, or that in your support corpus "sync" means a specific integration. Fine-tuning the embedding model, the reranker, or both is often the highest-leverage change in a mature RAG stack, and it is far cheaper than fine-tuning an LLM: the models are 100M–600M parameters and train in minutes to hours on one GPU.

## When off-the-shelf retrieval fails

Symptoms that point at the retriever rather than the generator:

- The right chunk exists but is not in the top 20 for queries that use internal jargon, product codes, acronyms or abbreviations.
- Queries are short and asymmetric (three-word searches against long technical documents), and the model ranks documents that share surface words above the one that answers the question.
- Your notion of relevance is domain-specific: two legal clauses are "similar" for a lawyer for reasons a general model does not see.
- Recall@k on a labeled set is much worse than on public benchmarks, and the gap persists across several off-the-shelf models.

Fix the cheap things first, in this order:

| Step | Cost | Fixes |
|---|---|---|
| Check chunking and parsing | Low | Answers split across chunks, tables mangled ([chunking]({{ '/llm/chunking/' | relative_url }})) |
| Add BM25 and fuse (hybrid) | Low | Exact identifiers, codes, rare terms |
| Add an off-the-shelf reranker | Low | Ordering within the top 50–100 |
| Query rewriting or expansion | Low–medium | Jargon, vague queries ([query transformation]({{ '/llm/query-transformation/' | relative_url }})) |
| Fine-tune the reranker | Medium | Domain relevance in the final ordering; no re-indexing |
| Fine-tune the embedding model | Medium–high | Candidates missing from the top-k entirely; requires re-embedding |

Fine-tune the reranker first if the right document is in the top 100 but not the top 5. Fine-tune the embedding model when it is not in the top 100 at all. See [hybrid search and reranking]({{ '/llm/hybrid-search-and-reranking/' | relative_url }}).

## How it works

### Bi-encoders and cross-encoders

```text
 Bi-encoder (embedding model)          Cross-encoder (reranker)

 query ---> [encoder] ---> q vec       [CLS] query [SEP] doc [SEP]
 doc   ---> [encoder] ---> d vec                  |
             score = cos(q, d)              [encoder, full attention
                                             across query and doc]
 docs embedded once, offline;                     |
 search = nearest neighbors (ANN)            relevance score
 fast, scales to millions               slow: one pass per pair,
                                        used on top 20-100 only
```

A **bi-encoder** maps query and document independently to vectors, so documents are embedded once and searched with an ANN index ([vector search]({{ '/llm/vector-search/' | relative_url }})). A **cross-encoder** reads query and document together, letting every query token attend to every document token. It is far more accurate per pair but costs a forward pass per pair, so it only reranks a shortlist. You can fine-tune either; the training data overlaps heavily.

### Contrastive learning and InfoNCE

Embedding models are trained contrastively: pull a query toward its relevant document, push it away from irrelevant ones. The standard loss is **InfoNCE**, which is just cross-entropy where the "classes" are candidate documents:

```text
loss_i = -log(  exp(s(q_i, d_i+) / tau)
              / sum_j exp(s(q_i, d_j) / tau) )

s = cosine similarity, tau = temperature (scale = 1 / tau)
```

The trick that makes it cheap is **in-batch negatives**: in a batch of `B` (query, positive) pairs, each query treats the other `B - 1` positives as negatives. You get `B - 1` negatives per query for free, which is why batch size matters so much for this loss: bigger batches mean more and harder negatives. sentence-transformers calls this `MultipleNegativesRankingLoss` (MNRL), with a default scale of 20 (temperature 0.05). Adding an explicit **hard negative** per query (a document that looks relevant but is not) adds the most informative negatives of all.

```python
import math


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(normalize(a), normalize(b)))


def info_nce(queries, positives, hard_negatives=(), scale=20.0) -> float:
    """Mean InfoNCE over a batch. Query i's positive is positives[i]; every
    other positive in the batch, plus each hard negative, is a negative."""
    candidates = list(positives) + list(hard_negatives)
    total = 0.0
    for i, q in enumerate(queries):
        logits = [scale * cos(q, c) for c in candidates]
        m = max(logits)
        log_z = m + math.log(sum(math.exp(x - m) for x in logits))
        total += log_z - logits[i]          # -log softmax at the positive
    return total / len(queries)


def recall_at_k(ranked: dict, relevant: dict, k: int) -> float:
    """Mean over queries of |relevant in top k| / |relevant|."""
    return sum(len(relevant[q] & set(r[:k])) / len(relevant[q])
               for q, r in ranked.items()) / len(ranked)


def mrr(ranked: dict, relevant: dict) -> float:
    """Mean reciprocal rank of the first relevant document."""
    total = 0.0
    for q, r in ranked.items():
        total += next((1 / i for i, d in enumerate(r, 1) if d in relevant[q]), 0.0)
    return total / len(ranked)


if __name__ == "__main__":
    qs = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
    pos = [[0.9, 0.1, 0, 0], [0.1, 0.9, 0, 0], [0, 0.1, 0.9, 0.1]]

    aligned = info_nce(qs, pos)
    shuffled = info_nce(qs, pos[1:] + pos[:1])   # wrong pairings
    assert aligned < 0.01 < shuffled

    # A hard negative close to query 0 raises the loss: that gap is the
    # training signal in-batch negatives alone would not provide.
    hard = info_nce(qs, pos, hard_negatives=[[0.85, 0.15, 0.05, 0]])
    assert hard > aligned
    # Lower scale (higher temperature) gives a softer, less confident loss.
    assert info_nce(qs, pos, scale=1.0) > aligned
    print(f"aligned {aligned:.4f}  hard-neg {hard:.3f}  shuffled {shuffled:.2f}")

    relevant = {
        "q1": {"d3"},
        "q2": {"d7"},
        "q3": {"d1", "d9"},
    }
    before = {"q1": ["d5", "d2", "d3"], "q2": ["d1", "d4", "d8"],
              "q3": ["d1", "d2", "d6"]}
    after = {"q1": ["d3", "d5", "d2"], "q2": ["d7", "d1", "d4"],
             "q3": ["d9", "d1", "d2"]}
    assert abs(recall_at_k(before, relevant, 3) - 0.5) < 1e-9
    assert recall_at_k(after, relevant, 3) == 1.0
    assert recall_at_k(after, relevant, 1) > recall_at_k(before, relevant, 1)
    assert mrr(after, relevant) == 1.0 and mrr(before, relevant) < 0.5
    print(f"recall@3 {recall_at_k(before, relevant, 3):.2f} -> "
          f"{recall_at_k(after, relevant, 3):.2f}, "
          f"MRR {mrr(before, relevant):.2f} -> {mrr(after, relevant):.2f}")
```

### Loss options

| Loss (sentence-transformers name) | Data | Notes |
|---|---|---|
| `MultipleNegativesRankingLoss` | (anchor, positive) or (anchor, positive, negative) | Default for retrieval; InfoNCE with in-batch negatives |
| `CachedMultipleNegativesRankingLoss` | Same | GradCache: large effective batch on small GPU memory, slower per step |
| `TripletLoss` | (anchor, positive, negative) | Margin-based; one negative at a time, weaker signal |
| `ContrastiveLoss`, `OnlineContrastiveLoss` | (a, b, label 0/1) | Pair classification data, duplicate detection |
| `CoSENTLoss`, `CosineSimilarityLoss` | (a, b, score) | Graded similarity labels (STS-style) |
| `MatryoshkaLoss` (wrapper) | Any of the above | Makes truncated prefixes of the vector usable (e.g. 768 down to 256 dims) |
| `MarginMSELoss` | (query, pos, neg, teacher margin) | Distills a cross-encoder's scores into a bi-encoder |

For cross-encoders the usual choices are binary cross-entropy on (query, passage, 0/1) pairs, or listwise losses over one positive and several negatives per query.

## Training data

### Where pairs come from

| Source | Pair | Quality |
|---|---|---|
| Search logs with clicks or dwell time | query, clicked doc | Plentiful, noisy, position-biased |
| Support tickets resolved by a KB article | ticket text, article | Excellent, real asymmetry |
| FAQ and help-center structure | question, answer | Clean but small |
| Title and body, heading and section | title, body | Free, weak signal |
| LLM-generated queries per chunk | synthetic query, source chunk | Scales to any corpus; filter hard |

**Synthetic queries** are the workhorse when you have no logs: ask an LLM to write two or three realistic questions each chunk answers, in the style of your users (short, misspelled, jargon-heavy if that is what users type). Then filter: keep a pair only if the source chunk ranks in the top few for its query under a cross-encoder or the current model (round-trip filtering), which removes vague questions that many chunks could answer. Methods like GPL formalize this for domain adaptation. A few thousand to tens of thousands of pairs is a typical useful range.

### Hard negatives

Random negatives are too easy; the model already separates "refund policy" from "office parking". Hard negatives are documents that look relevant but are not:

1. Retrieve the top-k for each query with BM25 or the current embedding model.
2. Drop the known positives.
3. Skip the very top ranks or apply a score ceiling, because those often contain **false negatives**: documents that actually answer the query but were never labeled. Training against them actively damages the model.
4. Optionally score candidates with a strong cross-encoder and keep only ones it rates clearly irrelevant.
5. Sample one to a few negatives per query from the remaining window (for example ranks 10–50).

sentence-transformers ships a helper for this (illustrative; parameter names vary across versions):

```python
from sentence_transformers.util import mine_hard_negatives

# pairs: Dataset with (query, positive) columns
triplets = mine_hard_negatives(
    pairs, model,
    range_min=10, range_max=50,   # skip likely false negatives near the top
    num_negatives=1,
)
```

### Splits and leakage

Split by document, not by row. If the same chunk appears in training pairs and eval pairs, you are measuring memorization. Hold out a set of documents (and their queries) entirely, plus a slice of real user queries the synthetic generator never saw.

## In practice

### Fine-tuning a bi-encoder with sentence-transformers

Illustrative, sentence-transformers v3+ API. Column order matters, names do not: (anchor, positive, negative).

```python
from datasets import load_dataset
from sentence_transformers import (
    SentenceTransformer, SentenceTransformerTrainer,
    SentenceTransformerTrainingArguments,
)
from sentence_transformers.evaluation import InformationRetrievalEvaluator
from sentence_transformers.losses import MultipleNegativesRankingLoss
from sentence_transformers.training_args import BatchSamplers

model = SentenceTransformer("BAAI/bge-base-en-v1.5")
train = load_dataset("json", data_files="triplets.jsonl", split="train")

evaluator = InformationRetrievalEvaluator(
    queries=dev_queries,          # dict qid -> text
    corpus=dev_corpus,            # dict doc_id -> text
    relevant_docs=dev_relevant,   # dict qid -> set of doc_ids
    name="support-dev",
)
evaluator(model)                  # baseline numbers before training

args = SentenceTransformerTrainingArguments(
    output_dir="bge-support-ft",
    num_train_epochs=1,
    per_device_train_batch_size=128,   # more in-batch negatives
    learning_rate=2e-5,
    warmup_ratio=0.1,
    bf16=True,
    batch_sampler=BatchSamplers.NO_DUPLICATES,  # avoid false in-batch negs
    eval_strategy="steps",
    eval_steps=200,
)
trainer = SentenceTransformerTrainer(
    model=model, args=args, train_dataset=train,
    loss=MultipleNegativesRankingLoss(model), evaluator=evaluator,
)
trainer.train()
model.save_pretrained("bge-support-ft/final")
```

Keep whatever query and document prefixes the base model expects (E5 uses `query: ` and `passage: `; BGE recommends an instruction prefix on queries for retrieval). Train with the same convention you will use at inference.

### Fine-tuning a reranker

Illustrative, sentence-transformers v4+ cross-encoder API. Rows are (query, passage, label) with label 1.0 for relevant and 0.0 for hard negatives.

```python
from sentence_transformers.cross_encoder import (
    CrossEncoder, CrossEncoderTrainer, CrossEncoderTrainingArguments,
)
from sentence_transformers.cross_encoder.losses import BinaryCrossEntropyLoss

reranker = CrossEncoder("BAAI/bge-reranker-base", num_labels=1)
args = CrossEncoderTrainingArguments(
    output_dir="reranker-support-ft",
    num_train_epochs=1,
    per_device_train_batch_size=32,
    learning_rate=2e-5,
    bf16=True,
)
trainer = CrossEncoderTrainer(
    model=reranker, args=args, train_dataset=labeled_pairs,
    loss=BinaryCrossEntropyLoss(reranker),
)
trainer.train()

scores = reranker.predict([(query, doc) for doc in candidates])
```

A fine-tuned reranker needs no re-indexing: swap the model and the next query uses it. That makes it the lower-risk first move.

### Evaluating before and after

Measure retrieval in isolation, then end to end:

| Metric | Answers | Use for |
|---|---|---|
| Recall@k (k = what you pass to the reranker or LLM) | Did the right chunk make the cut? | Embedding model, first stage |
| MRR@10 | How high is the first relevant hit? | Reranker, single-answer queries |
| nDCG@10 | Is the ordering right with graded relevance? | Reranker, multi-relevant queries |
| End-to-end answer quality | Did the final answer improve? | Confirms the retrieval gain matters ([RAG evaluation]({{ '/llm/rag-evaluation/' | relative_url }})) |

Report the baseline and fine-tuned numbers on the same held-out set, broken down by query type (jargon, identifiers, natural-language questions). If the model is also used outside this domain, check a general set too: aggressive fine-tuning can degrade general retrieval. A typical healthy result is a solid jump in recall@10 on the domain set with flat general performance; a gain that appears only on synthetic queries is not real.

### Re-embedding cost and rollout

A fine-tuned embedding model produces a new vector space. Old and new vectors are not comparable, so **every document must be re-embedded** before the new model serves a single query. Worked numbers for 5M chunks of about 400 tokens (2B tokens):

| Item | Estimate |
|---|---|
| Self-hosted, assume ~20k tokens/s per GPU for a base-size model | 2e9 / 2e4 = 100,000 s, about 28 GPU-hours, parallelizable |
| Hosted embedding API at ~$0.02 per 1M tokens (small model, 2025 pricing) | about $40, plus rate-limit time |
| Raw vectors, 768 dims, float32 | 5M x 768 x 4 B = 15.4 GB, doubled during migration |
| HNSW index build | hours at this size; needs generous `maintenance_work_mem` |

Measure your own throughput; it varies with model size, sequence length and batch size. Roll out like a schema migration (see [pgvector]({{ '/postgresql/pgvector/' | relative_url }})):

```sql
ALTER TABLE chunks ADD COLUMN embedding_v2 vector(768);
-- backfill embedding_v2 in batches from a background worker
CREATE INDEX CONCURRENTLY chunks_embedding_v2_hnsw
    ON chunks USING hnsw (embedding_v2 vector_cosine_ops);
-- shadow-read both, compare recall on live queries, flip a flag,
-- then drop the old column and index
```

New documents written during the backfill must be embedded with both models. If re-embedding is too expensive, a cheaper option is a **query-side adapter**: keep document vectors fixed and train a small linear transform applied only to query embeddings. Gains are smaller than full fine-tuning, but nothing gets re-indexed.

## Pitfalls

- **False negatives in mining.** Unlabeled relevant documents used as negatives teach the model to push away correct answers. Symptom: recall drops on queries with many relevant docs. Fix: skip top ranks, use a cross-encoder threshold.
- **Duplicates in a batch.** Two rows with the same positive make one row's positive another row's negative. Use the no-duplicates batch sampler.
- **Small batches with MNRL.** Batch size 8 gives 7 easy negatives. Use the cached variant to reach hundreds.
- **Evaluating on synthetic queries only.** The model learns the generator's phrasing. Keep real queries in the eval set.
- **Leakage via shared documents.** Same chunks in train and eval inflate recall. Split by document.
- **Prefix mismatch.** Training without the model's query prefix and serving with it (or the reverse) silently costs recall.
- **Forgetting the index.** Swapping the model without re-embedding returns garbage, because query and document vectors are from different spaces. Version the embedding model alongside every vector.
- **Overfitting with many epochs.** One to three epochs is typical; watch the dev evaluator and stop when recall plateaus.

## Checklist

- Labeled eval set of real queries with relevance judgments, split by document.
- Baseline recall@k, MRR and nDCG recorded for current model, BM25 and hybrid.
- Cheap fixes (chunking, hybrid, off-the-shelf reranker) tried first.
- Pairs from logs or filtered synthetic queries; hard negatives mined from a rank window.
- Reranker fine-tuned before the embedding model when candidates are in the top 100.
- Re-embedding cost, storage and migration plan approved before training the bi-encoder.
- Embedding model version stored with each vector; dual-write during backfill.

## Interview questions

**Why fine-tune an embedding model instead of the LLM?** Retrieval failures cap RAG quality, and embedding models are small (100M–600M parameters), cheap to train and directly measurable with recall@k. Fixing retrieval often improves answers more than tuning the generator.

**Explain in-batch negatives.** In a batch of B query-positive pairs, each query uses the other B-1 positives as negatives in a softmax cross-entropy (InfoNCE). It gives many negatives for free, so larger batches usually help.

**What are hard negatives and what can go wrong?** Documents that score highly for a query but are not relevant. They give the strongest learning signal, but mined top results can be unlabeled true positives, which teaches the model to reject correct answers. Skip the top ranks and filter with a cross-encoder.

**Bi-encoder vs cross-encoder?** Bi-encoders embed independently and support ANN search over millions of documents; cross-encoders attend jointly over the pair and are more accurate but cost one forward pass per pair, so they rerank a shortlist.

**What does temperature do in InfoNCE?** It scales similarities before the softmax. Low temperature (high scale) sharpens the distribution and focuses on the hardest negatives; too low makes training unstable, too high makes the loss uninformative.

**What does it cost to switch embedding models?** Re-embedding the entire corpus, rebuilding ANN indexes, temporarily doubling vector storage, and a dual-write migration, because vectors from different models are not comparable.

**How do you show the fine-tune worked?** Recall@k and nDCG on a held-out set of real queries, split by document, before and after, by query slice, plus an end-to-end RAG evaluation to confirm the retrieval gain changes answers.

## Further reading

- "Representation Learning with Contrastive Predictive Coding", van den Oord et al., 2018 (InfoNCE).
- "Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks", Reimers and Gurevych, 2019.
- "Dense Passage Retrieval for Open-Domain Question Answering", Karpukhin et al., 2020.
- "GPL: Generative Pseudo Labeling for Unsupervised Domain Adaptation of Dense Retrieval", Wang et al., 2022.
- "Matryoshka Representation Learning", Kusupati et al., 2022.
- sentence-transformers documentation: training overview, losses, and cross-encoder training.
