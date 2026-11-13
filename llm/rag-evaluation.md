---
title: Evaluating RAG
section: llm
order: 29
phase: "Phase 5 · Evaluation"
summary: Measure retrieval and generation separately with recall@k, MRR, nDCG, faithfulness and context recall, and pin every wrong answer on the stage that failed.
---

Without evaluation, every RAG change is a guess. A new chunker, embedding model, reranker, prompt or LLM version may help some queries and silently break others. The key idea is to split the pipeline into stages and measure each one, because a wrong answer has very different causes: the evidence was never indexed, it was indexed but not retrieved, it was retrieved but ranked out of the prompt, or it reached the prompt and the model ignored or distorted it. Each cause has a different fix, and an end-to-end score alone cannot tell you which one you have. This note builds on [Evaluating LLM systems]({{ '/llm/llm-evaluation/' | relative_url }}) (golden sets, judges, error bars) and applies it to [RAG pipelines]({{ '/llm/rag-architecture/' | relative_url }}).

## How it works

```text
query ─► first-stage ─► rerank ─► assemble ─► generate ─► answer
         retrieval      top-k     context
         top-50
            │             │          │            │
        recall@50     recall@5    evidence     faithfulness
                      MRR, nDCG   in prompt?   relevance
                      context                  correctness
                      precision                abstention

 before all of it: is the evidence in the index at all? (ingestion)
```

Two layers of metrics:

- **Retrieval metrics** need labels: for each query, which documents or chunks are relevant. They are cheap (no LLM calls), deterministic and fast enough to run on every PR.
- **Generation metrics** judge the answer against the retrieved context and, where you have one, a reference answer. They usually need an LLM judge or an NLI model, so they cost money and need calibration.

## Retrieval metrics

| Metric | Definition | Use it for |
|---|---|---|
| `recall@k` | Fraction of relevant items found in the top `k` | Did the evidence reach the prompt? The primary RAG metric |
| `precision@k` | Fraction of the top `k` that is relevant | Context noise and wasted tokens |
| `hit@k` | 1 if any relevant item is in the top `k` | Questions answerable from a single passage |
| MRR | Mean of `1 / rank` of the first relevant item | How high the first good hit sits |
| nDCG@k | Rank-discounted gain with graded relevance, normalized by the ideal ranking | Relevance with levels (perfect, partial) |
| Context precision | Mean of `precision@i` at each rank `i` holding a relevant item | Whether relevant chunks are ranked first |

nDCG in plain text: `DCG@k = sum over ranks i of (2^rel_i - 1) / log2(i + 1)`, and `nDCG@k = DCG@k / IDCG@k`, where IDCG is the DCG of the best possible ordering of all labeled items. Some libraries use `rel_i` instead of `2^rel_i - 1` as the gain; the two agree for binary labels, so state which one you report.

Measure at two depths:

- **First-stage recall at a large `k`** (`recall@50`): the ceiling for everything downstream. If it is low, fix chunking, embeddings, hybrid search, [query transformation]({{ '/llm/query-transformation/' | relative_url }}) or metadata filters.
- **Final recall at the prompt size** (`recall@5` after reranking): if `recall@50` is high and `recall@5` low, the [reranker]({{ '/llm/hybrid-search-and-reranking/' | relative_url }}) is the problem.

### Labels that survive re-chunking

Chunk-level labels break every time you change [chunking]({{ '/llm/chunking/' | relative_url }}). Label at the document level plus a short **evidence quote**, and count a chunk as relevant if it contains the quote (after whitespace normalization). The same labels then evaluate any chunker.

Beware **pooling bias**. If labels came from the top results of your current retriever, a new retriever that finds a different relevant document gets marked wrong for it. When comparing retrievers, label the union of top results from all candidates (the TREC pooling method), or have a judge grade unlabeled retrieved chunks and spot-check those grades.

### Runnable retrieval metrics

```python
from collections import defaultdict
from math import log2


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return len(set(retrieved[:k]) & relevant) / len(relevant) if relevant else 0.0


def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return len(set(retrieved[:k]) & relevant) / k


def hit_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    return float(any(d in relevant for d in retrieved[:k]))


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    return next((1 / i for i, d in enumerate(retrieved, 1) if d in relevant), 0.0)


def ndcg_at_k(retrieved: list[str], grades: dict[str, int], k: int) -> float:
    dcg = sum((2 ** grades.get(d, 0) - 1) / log2(i + 1) for i, d in enumerate(retrieved[:k], 1))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2**g - 1) / log2(i + 1) for i, g in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def context_precision(retrieved: list[str], relevant: set[str], k: int) -> float:
    hits, total = 0, 0.0
    for i, d in enumerate(retrieved[:k], 1):
        if d in relevant:
            hits += 1
            total += hits / i
    return total / hits if hits else 0.0


def evaluate(cases: list[dict], run, ks=(5, 50)) -> dict[str, dict[str, float]]:
    """cases: query, relevant (id -> grade), tags. run: query -> ranked ids."""
    by_tag = defaultdict(list)
    for c in cases:
        got, grades = run(c["query"]), c["relevant"]
        rel = set(grades)
        row = {f"recall@{k}": recall_at_k(got, rel, k) for k in ks}
        row |= {"mrr": reciprocal_rank(got, rel), "ndcg@10": ndcg_at_k(got, grades, 10)}
        for tag in ["all", *c["tags"]]:
            by_tag[tag].append(row)
    return {t: {m: sum(r[m] for r in rows) / len(rows) for m in rows[0]}
            for t, rows in by_tag.items()}


if __name__ == "__main__":
    retrieved = ["d3", "d1", "d7", "d2", "d9"]
    grades = {"d1": 3, "d2": 2, "d4": 1}           # graded labels; d4 never retrieved
    rel = set(grades)
    assert abs(recall_at_k(retrieved, rel, 5) - 2 / 3) < 1e-9
    assert precision_at_k(retrieved, rel, 5) == 0.4
    assert hit_at_k(retrieved, rel, 1) == 0.0 and hit_at_k(retrieved, rel, 2) == 1.0
    assert reciprocal_rank(retrieved, rel) == 0.5
    dcg = 7 / log2(3) + 3 / log2(5)
    idcg = 7 / log2(2) + 3 / log2(3) + 1 / log2(4)
    assert abs(ndcg_at_k(retrieved, grades, 5) - dcg / idcg) < 1e-9      # about 0.61
    assert context_precision(retrieved, rel, 5) == 0.5                   # (1/2 + 2/4) / 2

    index = {"refund window": ["d1", "d5", "d2"], "sso setup": ["d8", "d9", "d6"]}
    cases = [
        {"query": "refund window", "relevant": {"d1": 2, "d2": 1}, "tags": ["billing"]},
        {"query": "sso setup", "relevant": {"d6": 2}, "tags": ["auth"]},
    ]
    report = evaluate(cases, lambda q: index[q], ks=(1, 3))
    assert report["billing"]["recall@1"] == 0.5 and report["auth"]["recall@1"] == 0.0
    assert report["all"]["recall@3"] == 1.0 and abs(report["auth"]["mrr"] - 1 / 3) < 1e-9
    print({t: {m: round(v, 2) for m, v in r.items()} for t, r in report.items()})
```

Aggregate per tag as well as overall. A change that improves the average and breaks every "negation" or "exact identifier" query is a regression.

## Generation metrics

| Metric | Question it answers | Needs | Usually computed by |
|---|---|---|---|
| Faithfulness (groundedness) | Is every claim in the answer supported by the retrieved context? | Answer, contexts | Split answer into claims; judge or NLI per claim |
| Answer relevance | Does the answer address the question asked? | Question, answer | Judge; or generate questions from the answer and compare embeddings with the original |
| Context precision | Are the useful chunks ranked at the top? | Question, contexts, reference or labels | Per-chunk relevance verdict, then rank-weighted precision |
| Context recall | Does the context contain everything the reference answer needs? | Contexts, reference answer | Split the reference into claims; check each is attributable to the context |
| Answer correctness | Does the answer match the reference? | Answer, reference | Judge; exact match or token F1 for short answers |
| Citation accuracy | Do cited passages support the sentences that cite them? | Answer with citations, contexts | Judge or NLI per citation |
| Abstention | Does it decline when the sources lack the answer? | Unanswerable cases | Refusal classifier or judge |

Faithfulness and correctness are different axes. An answer can be perfectly faithful to a stale or wrong passage, and a correct answer can be unfaithful when the model answered from its own memory. You want both: faithfulness catches hallucination, correctness catches bad sources and retrieval misses.

Claim-level faithfulness works better than a holistic score:

```text
You are checking whether claims are supported by sources.

For each numbered claim, reply SUPPORTED, CONTRADICTED or NOT_FOUND, plus
the id of the source that decides it. Judge only against the sources below,
not your own knowledge. A claim that adds a detail absent from the sources
is NOT_FOUND, even if it is plausible.

<sources>
[S1] Annual plans are refundable within 30 days of purchase.
[S2] Refunds are issued to the original payment method.
</sources>
<claims>
1. Annual plans can be refunded within 30 days.
2. Refunds take 5 business days to arrive.
</claims>
```

`faithfulness = supported_claims / total_claims`. Track the share of answers containing any `CONTRADICTED` claim separately; those are the dangerous ones. A 0.9 average faithfulness can hide that 4% of answers state the opposite of the policy.

For abstention, keep 10–20% of the golden set unanswerable from your corpus and measure both sides: the share of unanswerable questions the system declines (missing this means hallucination) and the share of answerable questions it wrongly declines (over-refusal). Prompt changes that push one usually move the other.

## Synthetic test set generation

When you have few real queries, generate them from your corpus:

1. Sample documents and chunks, stratified by source, type and recency.
2. Ask an LLM for a question answerable from the passage, the reference answer and the supporting quote.
3. Evolve some into harder forms: multi-hop (combine two passages), conditional, comparison, and rewrites in a user's voice.
4. Filter with a critic: answerable, standalone (no "according to the document"), not duplicate.
5. Review a sample by hand and tag every case as synthetic so you can report it separately.

Ragas ships a test set generator that follows this pattern; you can also write it in a page of code. Know the biases before trusting the numbers:

| Bias | Why it happens | Mitigation |
|---|---|---|
| Lexical overlap | Questions reuse the passage's wording, which flatters both BM25 and dense retrieval | Rewrite in user vocabulary; measure question-to-passage token overlap against real queries |
| Single-passage questions | One passage in, one question out | Add multi-hop, aggregation ("how many", "compare") and unanswerable cases deliberately |
| Coverage skew | Generators favor clean, fact-dense prose and skip tables, scans and code | Stratify sampling by document type |
| Chunker coupling | Questions built from current chunks favor the current chunker | Label documents plus evidence quotes |
| Unnatural phrasing | "What does section 4.2 state about…" | Critic filter; few-shot with real queries |
| Wrong answer keys | The generator misreads the passage | Human review of a sample; judge disagreements flag key errors |
| Model-family advantage | The same family writes and answers the questions | Generate with a different family from the one under test |

Synthetic sets are good for coverage and regression detection. They overstate absolute quality. Report real-query and synthetic results separately, and replace synthetic cases with production queries as traffic arrives.

## Component vs end-to-end evals

| | Component evals | End-to-end evals |
|---|---|---|
| Scope | One stage: parser, chunker, retriever, reranker, or generator with fixed context | Query in, answer out, through the real pipeline |
| Labels | Relevant documents and evidence; fixed contexts | Reference answers or rubrics |
| Cost | Retrieval metrics need no LLM calls | System plus judge calls per case |
| Tells you | Where the problem is | Whether users get good answers |
| Blind spot | Stage interactions, such as a better reranker whose results then get truncated | Which stage caused a failure |

You need both. Three controlled experiments isolate the generator:

- **Oracle context:** feed the gold evidence directly. This is the generation ceiling. If oracle accuracy is 92% and end-to-end is 70%, about 22 points are lost before generation.
- **No context:** ask with an empty context. If the model already answers most questions from memory, your eval is not testing retrieval; add questions about private or recent content.
- **Gold plus distractors:** gold evidence buried among plausible but irrelevant chunks, in different positions. This measures robustness to noise and to position, since models use information in the middle of long contexts less reliably than at the edges.

## Diagnosing which stage failed

For each failing case, find the first stage that lost the evidence:

```text
evidence in the index?        no ─► ingestion: parsing, OCR, ACLs, sync
   │ yes
in first-stage top-50?        no ─► retrieval: embeddings, BM25, filters,
   │ yes                            query rewrite
in final top-k after rerank?  no ─► reranking
   │ yes
actually in the prompt?       no ─► context assembly: token budget,
   │ yes                            dedup, truncation, ordering
every claim supported?        no ─► generation: unfaithful
   │ yes
matches the reference?        no ─► generation: incomplete or misread
   │ yes                            (or a wrong reference or judge)
  pass
```

```python
from collections import Counter


def diagnose(c: dict, first_k: int = 50, final_k: int = 5) -> str:
    """Return the first pipeline stage that lost the evidence."""
    if not c["evidence_indexed"]:
        return "ingestion"
    if c["first_stage_rank"] is None or c["first_stage_rank"] > first_k:
        return "retrieval"
    if c["final_rank"] is None or c["final_rank"] > final_k:
        return "reranking"
    if not c["evidence_in_prompt"]:
        return "context assembly"
    if c["unsupported_claims"] > 0:
        return "generation: unfaithful"
    if not c["correct"]:
        return "generation: incomplete"
    return "pass"


def case(indexed=True, first=1, final=1, in_prompt=True, unsupported=0, correct=True) -> dict:
    return {"evidence_indexed": indexed, "first_stage_rank": first, "final_rank": final,
            "evidence_in_prompt": in_prompt, "unsupported_claims": unsupported,
            "correct": correct}


if __name__ == "__main__":
    results = [
        case(),
        case(indexed=False, first=None, final=None),   # scanned PDF never OCRed
        case(first=None, final=None),                  # vocabulary mismatch
        case(first=12, final=9),                       # reranker pushed it out
        case(first=3, final=4, in_prompt=False),       # truncated by token budget
        case(unsupported=2, correct=False),            # invented a fee
        case(correct=False),                           # answered half the question
        case(),
    ]
    counts = Counter(diagnose(c) for c in results)
    assert counts["pass"] == 2 and counts["ingestion"] == 1 and counts["reranking"] == 1
    assert counts["context assembly"] == 1 and counts["generation: unfaithful"] == 1
    assert sum(counts.values()) == len(results)
    print(counts.most_common())
```

To make this possible, log per request: retrieved ids and scores at each stage, the final prompt's chunk ids, and the answer. Then most fields above come from the trace, and only `unsupported_claims` and `correct` need a judge. When failures cluster in one stage, fix that stage first; when the "generation: incomplete" bucket is large, read those cases before blaming the model, because a wrong reference or a miscalibrated judge often lands there.

## In practice

### Golden set record

```json
{
  "id": "refund-annual-45d",
  "tenant": "eval-tenant-1",
  "query": "Can I get a refund on an annual plan after 45 days?",
  "relevant_docs": {"policy/refunds.md": 2, "faq/billing.md": 1},
  "evidence": ["Annual plans are refundable within 30 days"],
  "reference_answer": "No. Annual plans are refundable only within 30 days.",
  "tags": ["billing", "negation"],
  "answerable": true,
  "source": "support-ticket"
}
```

Use dedicated eval tenants with fixed fixtures so ACLs and content changes in real tenants do not move the numbers.

### Ragas (illustrative)

Ragas implements faithfulness, response relevancy, context precision and context recall with an LLM judge. Class and column names have changed across versions (older releases used `question`, `answer`, `contexts`, `ground_truth`), so check the docs for the version you install:

```python
# Illustrative, Ragas 0.2-style API; `judge_llm` is a Ragas-wrapped evaluator model.
from ragas import EvaluationDataset, evaluate
from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall

rows = [
    {
        "user_input": "Can I get a refund on an annual plan after 45 days?",
        "retrieved_contexts": ["Annual plans are refundable within 30 days of purchase."],
        "response": "No. Annual plans can be refunded only within 30 days.",
        "reference": "No. Annual plans are refundable only within 30 days.",
    },
]
result = evaluate(
    dataset=EvaluationDataset.from_list(rows),
    metrics=[Faithfulness(), LLMContextPrecisionWithReference(), LLMContextRecall()],
    llm=judge_llm,
)
print(result.to_pandas())
```

Treat library metrics like any judge: calibrate them against a few dozen human labels on your data before gating on them.

### Regression testing in CI

```text
PR changes prompt / chunker / embeddings / reranker / retrieval params
        │
        ▼
  retrieval metrics on the full golden set     (no LLM calls: every PR)
  generation judges on the full set            (nightly, or PR label "eval")
        │
        ▼
  compare per case with the baseline from main
  fail if: significant paired drop in recall@5 or faithfulness,
           any "critical" case flips to fail, or a tag falls below floor
```

Store per-case results, not just averages, so you can diff which queries changed. Pin the judge model and prompt; a judge change invalidates the baseline. Reindexing with a new embedding model is a change like any other and goes through the same gate.

> **In production:** Offline sets miss what users actually ask. Log retrieval ids and scores, track "no answer" and negative-feedback rates per topic, and sample production queries into the golden set weekly. See [Online evaluation and A/B testing]({{ '/llm/online-evaluation/' | relative_url }}).

## Pitfalls

- **Only measuring final answers.** You cannot separate retrieval misses from generation errors. Fix: stage metrics and stage attribution.
- **Synthetic-only golden sets.** Inflated retrieval scores from lexical overlap. Fix: mix in real queries and report them separately.
- **Chunk-level labels that rot.** Re-chunking invalidates them. Fix: document plus evidence-quote labels.
- **Pooling bias.** A new retriever is penalized for relevant documents nobody labeled. Fix: pool candidates from all systems, or judge-grade unlabeled hits.
- **Questions the model answers from memory.** High scores that do not depend on retrieval. Fix: run a no-context baseline; add private and recent-content questions.
- **Faithfulness without correctness.** Faithful answers built on stale documents. Fix: track both, plus document freshness.
- **Evaluating on live tenant data.** ACLs and content drift move the numbers. Fix: fixed eval tenants.

## Checklist

- Golden set in git with document plus evidence labels, graded relevance, reference answers, tags and 10–20% unanswerable cases.
- `recall@50`, `recall@5`, MRR and nDCG on every retrieval change, per tag.
- Claim-level faithfulness, context recall, correctness and abstention on a schedule, with calibrated judges.
- Oracle-context and no-context baselines measured once per model change.
- Traces log ids per stage so every failure gets a stage label.
- Real and synthetic results reported separately; production queries flow in weekly.

## Interview questions

**How do you evaluate a RAG system?** Split it: retrieval metrics (recall@k, MRR, nDCG) against labeled evidence, and generation metrics (faithfulness, correctness, abstention) with calibrated judges. Use a golden set of real and synthetic queries, gated in CI, plus stage attribution for failures.

**Recall@50 is high but answers are bad. Where do you look?** Check recall@5 after reranking. If that is fine, check whether the evidence actually reaches the prompt (token budget, truncation), then faithfulness. An oracle-context run tells you whether the generator can answer at all.

**What is the difference between faithfulness and answer correctness?** Faithfulness asks whether the answer is supported by the retrieved context; correctness asks whether it matches the truth. An answer can be faithful to a stale document and still be wrong.

**Explain nDCG and when you would use it over recall.** nDCG discounts gain by rank and normalizes by the ideal ordering, so it rewards placing highly relevant items first and supports graded labels. Use it when order within the prompt matters or relevance has levels; recall@k only asks whether items made the cut.

**What are the risks of synthetic test sets?** Lexical overlap that flatters retrieval, single-passage bias, coverage skew toward clean text, wrong answer keys and model-family advantage. Use them for coverage, rewrite in user vocabulary, and report them apart from real queries.

## Further reading

- Es et al., "Ragas: Automated Evaluation of Retrieval Augmented Generation" (2023).
- Saad-Falcon et al., "ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems" (2023).
- Järvelin and Kekäläinen, "Cumulated Gain-Based Evaluation of IR Techniques" (2002).
- Thakur et al., "BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models" (2021).
- Min et al., "FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation" (2023).
- Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" (2023).
